from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

import asyncssh
import click

from locki.cmd.cleanup import EXIT_VM_NOT_RUNNING, EXIT_VM_POWERED_OFF
from locki.paths import DATA, RUNTIME, STATE

HOST_KEY = STATE / "ssh" / "host_key"
CLIENT_KEY = DATA / "home" / ".ssh" / "id_locki"
AUTHORIZED_KEYS_FILE = STATE / "ssh" / "authorized_keys"
PID_FILE = RUNTIME / "daemon.pid"
PORT_FILE = RUNTIME / "daemon.port"

CLEANUP_INTERVAL = 60

logger = logging.getLogger(__name__)


async def _pump(reader, writer_write, writer_close) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer_write(data)
    except (asyncssh.BreakReceived, asyncssh.SignalReceived, asyncssh.TerminalSizeChanged):
        pass
    except Exception:
        logger.exception("pump failed")
    finally:
        with contextlib.suppress(Exception):
            writer_close()


@click.command("_daemon", hidden=True)
def daemon_cmd() -> None:
    """Host daemon: SSH forced-command proxy + periodic cleanup."""
    log_file = STATE / "logs" / "daemon.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

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

        async def handle_process(process: asyncssh.SSHServerProcess) -> None:
            try:
                env = os.environ.copy()
                env["SSH_ORIGINAL_COMMAND"] = process.command or ""
                sub = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "locki", "self-service",
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.gather(
                    _pump(process.stdin, sub.stdin.write, sub.stdin.close),
                    _pump(sub.stdout, process.stdout.write, process.stdout.close),
                    _pump(sub.stderr, process.stderr.write, process.stderr.close),
                )
                process.exit(await sub.wait() or 0)
            except Exception:
                logger.exception("SSH session failed")
                with contextlib.suppress(Exception):
                    process.exit(1)

        server = await asyncssh.listen(
            host="0.0.0.0",
            port=0,
            server_host_keys=[str(HOST_KEY)],
            authorized_client_keys=str(AUTHORIZED_KEYS_FILE),
            process_factory=handle_process,
            encoding=None,
            allow_scp=False,
            agent_forwarding=False,
            x11_forwarding=False,
        )
        port = next(iter(server.sockets)).getsockname()[1]
        PORT_FILE.write_text(str(port))
        PID_FILE.write_text(str(os.getpid()))
        logger.info("Locki daemon listening on 0.0.0.0:%d", port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        async def cleanup_loop() -> None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=CLEANUP_INTERVAL)
            while not stop.is_set():
                try:
                    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "locki", "cleanup")
                    rc = await proc.wait()
                    if rc in (EXIT_VM_POWERED_OFF, EXIT_VM_NOT_RUNNING):
                        logger.info("VM no longer running (cleanup rc=%d); daemon exiting.", rc)
                        stop.set()
                        return
                except Exception:
                    logger.exception("Cleanup tick failed")
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
