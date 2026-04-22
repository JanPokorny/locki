from __future__ import annotations

import json
import logging
import pathlib
import subprocess
import sys
import time

import click

from locki.paths import STATE, WORKTREES
from locki.utils import limactl

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 600
VM_IDLE_TIMEOUT = 600

LAST_ACTIVE_FILE = STATE / "cleanup" / "last-active.json"
VM_IDLE_SINCE_FILE = STATE / "cleanup" / "vm-idle-since"

EXIT_VM_POWERED_OFF = 2
EXIT_VM_NOT_RUNNING = 3


def _incus(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [limactl(), "shell", "--tty=false", "locki", "--", "sudo", "incus", *args],
        capture_output=True,
        text=True,
    )


def _list_containers() -> list[tuple[str, str]]:
    """Return (name, status) for every container."""
    pairs: list[tuple[str, str]] = []
    for line in _incus(["list", "--format=csv", "--columns=n,s"]).stdout.splitlines():
        name, _, status = line.partition(",")
        name, status = name.strip(), status.strip()
        if name:
            pairs.append((name, status))
    return pairs


@click.command(hidden=True)
def cleanup_cmd():
    """One-shot: stop idle containers, remove orphans, power off idle VM."""
    vm_list = subprocess.run([limactl(), "list", "--json"], capture_output=True, text=True)
    vm_running = False
    for line in vm_list.stdout.splitlines():
        try:
            vm = json.loads(line)
        except json.JSONDecodeError:
            continue
        if vm.get("name") == "locki" and vm.get("status") == "Running":
            vm_running = True
            break
    if not vm_running:
        sys.exit(EXIT_VM_NOT_RUNNING)

    try:
        last_active = json.loads(LAST_ACTIVE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        last_active = {}

    worktrees_root = WORKTREES.resolve()
    for name, _status in _list_containers():
        r = _incus(["config", "device", "get", name, "worktree", "source"])
        if r.returncode != 0 or not r.stdout.strip():
            continue
        source_path = pathlib.Path(r.stdout.strip()).resolve()
        if source_path.is_relative_to(worktrees_root) and not source_path.exists():
            logger.info("Deleting orphaned container %r (worktree %s is gone).", name, source_path)
            _incus(["delete", "--force", name])
            last_active.pop(name, None)

    running = {name for name, status in _list_containers() if status == "RUNNING"}
    active: set[str] = set()
    ops = _incus(["operation", "list", "--format=json"])
    if ops.returncode == 0 and ops.stdout.strip():
        try:
            for op in json.loads(ops.stdout):
                if op.get("status") != "Running":
                    continue
                for key in ("containers", "instances"):
                    for path in (op.get("resources") or {}).get(key) or []:
                        active.add(path.rsplit("/", 1)[-1])
        except json.JSONDecodeError:
            pass
    now = time.time()
    for name in running:
        if name in active or name not in last_active:
            last_active[name] = now
        elif now - last_active[name] >= IDLE_TIMEOUT:
            logger.info("Stopping idle container %r (idle %.0fs).", name, now - last_active[name])
            _incus(["stop", name])
            last_active.pop(name, None)
    for name in list(last_active):
        if name not in running:
            last_active.pop(name)

    LAST_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_ACTIVE_FILE.write_text(json.dumps(last_active))

    if any(status == "RUNNING" for _name, status in _list_containers()):
        VM_IDLE_SINCE_FILE.unlink(missing_ok=True)
        return

    try:
        idle_since = float(VM_IDLE_SINCE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        idle_since = now
        VM_IDLE_SINCE_FILE.write_text(str(now))
    if now - idle_since >= VM_IDLE_TIMEOUT:
        logger.info("No running containers for %.0fs — stopping VM.", now - idle_since)
        subprocess.run([limactl(), "stop", "locki"], capture_output=True)
        VM_IDLE_SINCE_FILE.unlink(missing_ok=True)
        sys.exit(EXIT_VM_POWERED_OFF)
