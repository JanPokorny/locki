#!/usr/bin/env python3
"""Stops idle Incus containers that have no active user sessions.
Also removes containers whose worktree directory no longer exists.
"""
import json
import logging
import pathlib
import signal
import subprocess
import sys
import time

IDLE_TIMEOUT = 600  # seconds
CHECK_INTERVAL = 60  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger(__name__)


def all_containers() -> list[str]:
    r = subprocess.run(
        ["incus", "list", "--format=csv", "--columns=n"],
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def running_containers() -> list[str]:
    r = subprocess.run(
        ["incus", "list", "--format=csv", "--columns=n,s"],
        capture_output=True,
        text=True,
    )
    names = []
    for line in r.stdout.splitlines():
        parts = line.split(",", 1)
        if len(parts) == 2 and parts[1].strip() == "RUNNING":
            names.append(parts[0].strip())
    return names


def containers_with_active_sessions() -> set[str]:
    """Return names of containers that have active exec operations."""
    r = subprocess.run(
        ["incus", "operation", "list", "--format=json"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return set()
    try:
        ops = json.loads(r.stdout)
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


def worktree_source(name: str) -> str | None:
    """Return the source path of the worktree disk device, or None if absent."""
    r = subprocess.run(
        ["incus", "config", "device", "get", name, "worktree", "source"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def stop_container(name: str) -> None:
    subprocess.run(["incus", "stop", name], capture_output=True)


def delete_container(name: str) -> None:
    subprocess.run(["incus", "delete", "--force", name], capture_output=True)


def remove_orphaned_containers(last_active: dict[str, float]) -> None:
    """Delete containers whose worktree directory no longer exists."""
    for name in all_containers():
        source = worktree_source(name)
        if source is None:
            continue  # no worktree device — not a locki container
        if not pathlib.Path(source).exists():
            log.info("Deleting orphaned container %r (worktree %s is gone).", name, source)
            delete_container(name)
            last_active.pop(name, None)


def main() -> None:
    last_active: dict[str, float] = {}

    def _exit(signum, frame):  # noqa: ANN001
        log.info("Received signal %d, exiting.", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)

    log.info(
        "locki-idle-daemon started (idle_timeout=%ds check_interval=%ds)",
        IDLE_TIMEOUT,
        CHECK_INTERVAL,
    )

    while True:
        try:
            remove_orphaned_containers(last_active)

            running = set(running_containers())
            active = containers_with_active_sessions()
            now = time.monotonic()

            for name in running:
                if name in active:
                    last_active[name] = now
                elif name not in last_active:
                    last_active[name] = now
                else:
                    idle_for = now - last_active[name]
                    if idle_for >= IDLE_TIMEOUT:
                        log.info("Stopping idle container %r (idle %.0fs).", name, idle_for)
                        stop_container(name)
                        last_active.pop(name, None)

            # Prune tracking for containers that are no longer running
            for name in list(last_active):
                if name not in running:
                    last_active.pop(name)

        except Exception as exc:
            log.error("Unexpected error: %s", exc)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
