"""Internal commands invoked by Locki itself — not for direct end-user use.

* `locki internal daemon` — long-running host daemon: asyncssh proxy that validates and
  runs bridged commands in-process, plus a periodic cleanup scheduler (stop idle
  containers, remove orphans, power off idle VM).
"""

import contextlib
import datetime
import functools
import json
import logging
import os
import pathlib
import shlex
import signal
import sys
import time

import click

from locki.logging import FILE_LOG_FORMAT
from locki.paths import (
    DENIED_LOG,
    PACKAGE_DATA,
    PID_FILE,
    PORT_FILE,
    RUNTIME,
    SANDBOX_HOME,
    STATE,
    WORKTREES,
    WORKTREES_META,
)
from locki.services.bridge import BridgeDeniedError, Ruleset
from locki.services.container import WORKTREE_DEVICE
from locki.services.daemon import VERSION, VERSION_FILE
from locki.services.vm import vm
from locki.services.worktree import worktrees, wt_id_from_dir
from locki.utils import AliasGroup

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 600
VM_IDLE_TIMEOUT = 600
CLEANUP_INTERVAL = 60

LAST_ACTIVE_FILE = STATE / "cleanup" / "last-active.json"
VM_IDLE_SINCE_FILE = STATE / "cleanup" / "vm-idle-since"
HOST_KEY = STATE / "ssh" / "host_key"
CLIENT_KEY = SANDBOX_HOME / ".ssh" / "id_locki"
AUTHORIZED_KEYS_FILE = STATE / "ssh" / "authorized_keys"


@click.group(cls=AliasGroup, help="Internal commands (invoked by Locki itself).", hidden=True)
def internal_app():
    pass


def _cleanup_once() -> None:
    """Stop idle containers, remove orphans, power off the idle VM."""
    if vm.status() != "Running":
        return

    try:
        last_active = json.loads(LAST_ACTIVE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        last_active = {}

    result = vm.incus(["list", "--format=json"])
    try:
        containers = json.loads(result.stdout) if result.returncode == 0 else []
    except json.JSONDecodeError:
        containers = []

    worktrees_root = WORKTREES.resolve()
    deleted: set[str] = set()
    for container in containers:
        name = container.get("name", "")
        source = ((container.get("expanded_devices") or {}).get(WORKTREE_DEVICE) or {}).get("source", "")
        if not name or not source:
            continue
        src = pathlib.Path(source).resolve()
        if src.is_relative_to(worktrees_root) and not src.exists():
            logger.info("Deleting orphaned container %r (worktree %s is gone).", name, src)
            vm.incus(["delete", "--force", name])
            deleted.add(name)
            last_active.pop(name, None)

    running = {c["name"] for c in containers if c.get("status", "").lower() == "running"} - deleted
    active: set[str] = set()
    ops = vm.incus(["operation", "list", "--format=json"])
    if ops.returncode == 0 and ops.stdout.strip():
        with contextlib.suppress(json.JSONDecodeError):
            for op in json.loads(ops.stdout):
                if op.get("status") == "Running":
                    for key in ("containers", "instances"):
                        for path in (op.get("resources") or {}).get(key) or []:
                            active.add(path.rsplit("/", 1)[-1])

    for name in active & running:
        worktrees.touch(name)  # keep last-used fresh through long unattended sessions

    now = time.time()
    stopped: set[str] = set()
    for name in running:
        if name in active or name not in last_active:
            last_active[name] = now
        elif now - last_active[name] >= IDLE_TIMEOUT:
            logger.info("Stopping idle container %r (idle %.0fs).", name, now - last_active[name])
            if vm.incus(["stop", name]).returncode == 0:
                stopped.add(name)
            last_active.pop(name, None)
    last_active = {n: t for n, t in last_active.items() if n in running}
    LAST_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_ACTIVE_FILE.write_text(json.dumps(last_active))

    if running - stopped:
        VM_IDLE_SINCE_FILE.unlink(missing_ok=True)
        return

    try:
        idle_since = float(VM_IDLE_SINCE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        idle_since = now
        VM_IDLE_SINCE_FILE.write_text(str(now))
    if now - idle_since >= VM_IDLE_TIMEOUT:
        logger.info("No running containers for %.0fs — stopping VM.", now - idle_since)
        vm.stop(force=False, check=False, quiet=True)
        VM_IDLE_SINCE_FILE.unlink(missing_ok=True)


@functools.cache
def _ruleset() -> Ruleset:
    """AGENTS.md is package data — static for this process's lifetime (an upgraded
    locki restarts the daemon via the version handshake in DaemonService)."""
    return Ruleset.from_markdown((PACKAGE_DATA / "AGENTS.md").read_text())


def _resolve_bridged(cmd: str) -> tuple[list[str], pathlib.Path, dict[str, str]]:
    """Validate a bridged command line and return (argv, cwd, env) to execute.

    Raises BridgeDeniedError for anything not allowed."""
    if not cmd:
        raise BridgeDeniedError("No command specified.")
    try:
        cwd_str, exe, *argv = shlex.split(cmd)
    except ValueError:
        raise BridgeDeniedError("Usage: <cwd> <exe> [args...]") from None

    cwd = pathlib.Path(cwd_str).resolve()
    wt_root = WORKTREES.resolve()
    if not cwd.is_relative_to(wt_root):
        raise BridgeDeniedError(f"Not inside a locki worktree: {str(cwd)!r}")
    rel_parts = cwd.relative_to(wt_root).parts
    if not rel_parts:
        raise BridgeDeniedError(f"Not inside a locki worktree: {str(cwd)!r}")
    wt_dir = rel_parts[0]
    wt_id = wt_id_from_dir(wt_dir)

    sandbox_root = WORKTREES / wt_dir
    p: pathlib.Path = cwd
    while True:
        git_file = p / ".git"
        if git_file.is_symlink():
            raise BridgeDeniedError(f"Refusing to follow symlinked .git at {str(git_file)!r}")
        if git_file.is_file():
            break
        if p == sandbox_root:
            raise BridgeDeniedError(f"No worktree .git found at or above {str(cwd)!r}")
        p = p.parent
    match p.relative_to(wt_root).parts:
        case [wt_dir_] if wt_dir_ == wt_dir:
            meta_git = WORKTREES_META / wt_dir / ".git"
        case [wt_dir_, ".locki", "include", included_wt_dir] if wt_dir_ == wt_dir:
            meta_git = WORKTREES_META / wt_dir / "include" / included_wt_dir / ".git"
        case _:
            raise BridgeDeniedError(f"Unexpected worktree layout: {p}")
    if not meta_git.exists():
        raise BridgeDeniedError(f"Missing worktree metadata: {meta_git}")

    if error := _ruleset().check([exe, *argv], wt_id, str(cwd)):
        with contextlib.suppress(OSError):
            DENIED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DENIED_LOG.open("a") as fh:
                fh.write(
                    f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{wt_id}\t{shlex.join([exe, *argv])}\n"
                )
        raise BridgeDeniedError(error)

    trusted = meta_git.read_text()
    if git_file.read_text() != trusted:
        fd = os.open(git_file, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        try:
            os.write(fd, trusted.encode())
        finally:
            os.close(fd)

    env = dict(os.environ)
    if exe == "locki":
        return [sys.executable, "-m", "locki", *argv], cwd, env
    if exe == "git":
        env["GIT_EDITOR"] = "true"
    return [exe, *argv], cwd, env


@internal_app.command("daemon")
def internal_daemon() -> None:
    """Host daemon: SSH forced-command proxy + periodic cleanup."""
    import asyncio

    import asyncssh

    log_file = STATE / "logs" / "daemon.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT))
    logging.getLogger().addHandler(handler)

    async def main() -> None:
        HOST_KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        CLIENT_KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (HOST_KEY, CLIENT_KEY):
            if not path.exists():
                key = asyncssh.generate_private_key("ssh-ed25519")
                key.write_private_key(str(path))
                key.write_public_key(str(path.with_suffix(".pub")))
                os.chmod(path, 0o600)
        AUTHORIZED_KEYS_FILE.write_text(CLIENT_KEY.with_suffix(".pub").read_text())
        os.chmod(AUTHORIZED_KEYS_FILE, 0o600)
        RUNTIME.mkdir(parents=True, exist_ok=True)

        async def handle(process: asyncssh.SSHServerProcess) -> None:
            try:
                try:
                    argv, cwd, env = await asyncio.to_thread(_resolve_bridged, process.command or "")
                except (BridgeDeniedError, SystemExit) as e:
                    # SystemExit covers fail()/sys.exit paths reached from validation
                    # (e.g. a missing binary); it must not take down the daemon.
                    msg = (
                        str(e)
                        if isinstance(e, BridgeDeniedError) or isinstance(e.code, str)
                        else "Bridged command failed."
                    )
                    process.stderr.write(f"{msg}\n".encode())
                    process.exit(1)
                    return
                try:
                    sub = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=cwd,
                        env=env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except FileNotFoundError:
                    process.stderr.write(f"{argv[0]}: command not found on host\n".encode())
                    process.exit(1)
                    return
                await process.redirect(stdin=sub.stdin, stdout=sub.stdout, stderr=sub.stderr)
                process.exit(await sub.wait() or 0)
            except Exception:
                logger.exception("SSH session failed")
                with contextlib.suppress(Exception):
                    process.exit(1)

        server = await asyncssh.listen(
            host="127.0.0.1",
            port=0,
            server_host_keys=[str(HOST_KEY)],
            authorized_client_keys=str(AUTHORIZED_KEYS_FILE),
            process_factory=handle,
            encoding=None,
            allow_scp=False,
            agent_forwarding=False,
            x11_forwarding=False,
        )
        port = next(iter(server.sockets)).getsockname()[1]
        PORT_FILE.write_text(str(port))
        PID_FILE.write_text(str(os.getpid()))
        VERSION_FILE.write_text(VERSION)
        logger.info("Locki daemon %s listening on 127.0.0.1:%d", VERSION, port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        async def cleanup_loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(_cleanup_once)
                except Exception:
                    logger.exception("Cleanup failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=CLEANUP_INTERVAL)

        cleanup_task = asyncio.create_task(cleanup_loop())
        await stop.wait()
        server.close()
        await server.wait_closed()
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    try:
        asyncio.run(main())
    finally:
        PID_FILE.unlink(missing_ok=True)
        PORT_FILE.unlink(missing_ok=True)
        VERSION_FILE.unlink(missing_ok=True)
