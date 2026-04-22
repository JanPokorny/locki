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

CLEANUP_STATE = STATE / "cleanup"
LAST_ACTIVE_FILE = CLEANUP_STATE / "last-active.json"
VM_IDLE_SINCE_FILE = CLEANUP_STATE / "vm-idle-since"

EXIT_OK = 0
EXIT_VM_POWERED_OFF = 2
EXIT_VM_NOT_RUNNING = 3


def _vm_running() -> bool:
    result = subprocess.run([limactl(), "list", "--json"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        try:
            vm = json.loads(line)
        except json.JSONDecodeError:
            continue
        if vm.get("name") == "locki" and vm.get("status") == "Running":
            return True
    return False


def _incus(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [limactl(), "shell", "--tty=false", "locki", "--", "sudo", "incus", *args],
        capture_output=True,
        text=True,
    )


def _list_containers() -> list[tuple[str, str]]:
    """Return (name, status) for every container."""
    result = _incus(["list", "--format=csv", "--columns=n,s"])
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        name, _, status = line.partition(",")
        name, status = name.strip(), status.strip()
        if name:
            pairs.append((name, status))
    return pairs


def _container_worktree_source(name: str) -> str | None:
    result = _incus(["config", "device", "get", name, "worktree", "source"])
    if result.returncode != 0:
        return None
    source = result.stdout.strip()
    return source or None


def _active_container_names() -> set[str]:
    """Names of containers that have a running incus operation attached."""
    result = _incus(["operation", "list", "--format=json"])
    if result.returncode != 0 or not result.stdout.strip():
        return set()
    try:
        ops = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    active: set[str] = set()
    for op in ops:
        if op.get("status") != "Running":
            continue
        resources = op.get("resources") or {}
        for key in ("containers", "instances"):
            for path in resources.get(key) or []:
                active.add(path.rsplit("/", 1)[-1])
    return active


@click.command(hidden=True)
def cleanup_cmd():
    """One-shot: stop idle containers, remove orphans, power off idle VM."""
    if not _vm_running():
        sys.exit(EXIT_VM_NOT_RUNNING)

    try:
        last_active = json.loads(LAST_ACTIVE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        last_active = {}

    worktrees_root = WORKTREES.resolve()
    for name, _status in _list_containers():
        source = _container_worktree_source(name)
        if source is None:
            continue
        source_path = pathlib.Path(source).resolve()
        # Only consider managed worktrees; never touch containers mounted elsewhere.
        if not source_path.is_relative_to(worktrees_root):
            continue
        if not source_path.exists():
            logger.info("Deleting orphaned container %r (worktree %s is gone).", name, source)
            _incus(["delete", "--force", name])
            last_active.pop(name, None)

    running = {name for name, status in _list_containers() if status == "RUNNING"}
    active = _active_container_names()
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

    CLEANUP_STATE.mkdir(parents=True, exist_ok=True)
    LAST_ACTIVE_FILE.write_text(json.dumps(last_active))

    still_running = {name for name, status in _list_containers() if status == "RUNNING"}
    if still_running:
        VM_IDLE_SINCE_FILE.unlink(missing_ok=True)
        sys.exit(EXIT_OK)

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
    sys.exit(EXIT_OK)
