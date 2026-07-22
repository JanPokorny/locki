import contextlib
import getpass
import importlib.metadata
import logging
import os
import signal
import subprocess
import sys
import time

from locki.paths import PACKAGE_DATA, PID_FILE, PORT_FILE, RUNTIME, SANDBOX_HOME
from locki.utils import file_lock

logger = logging.getLogger(__name__)

VERSION = importlib.metadata.version("locki")
VERSION_FILE = RUNTIME / "daemon.version"


class DaemonService:
    """The Locki host daemon (SSH forced-command proxy + periodic cleanup)."""

    def ensure_running(self) -> None:
        """Idempotently start the daemon and write the sandbox-side SSH config pointing at it."""
        RUNTIME.mkdir(parents=True, exist_ok=True)
        client_ssh_dir = SANDBOX_HOME / ".ssh"
        client_ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        ssh_port = 0
        with file_lock("daemon", "Waiting for daemon start"):
            alive = False
            pid = 0
            if PID_FILE.exists():
                with contextlib.suppress(ProcessLookupError, ValueError, PermissionError, FileNotFoundError):
                    pid = int(PID_FILE.read_text().strip())
                    os.kill(pid, 0)
                    alive = True
            # The daemon validates bridged commands in-process, so an upgraded locki
            # must restart a running daemon of another version to pick up new code.
            stored_version = ""
            with contextlib.suppress(OSError):
                stored_version = VERSION_FILE.read_text().strip()
            if alive and stored_version != VERSION:
                logger.info("Restarting locki daemon (version %r -> %r).", stored_version, VERSION)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(50):  # wait for its cleanup to unlink the pid/port files
                        os.kill(pid, 0)
                        time.sleep(0.1)
                alive = False
            if not alive:
                PID_FILE.unlink(missing_ok=True)
                PORT_FILE.unlink(missing_ok=True)
                subprocess.Popen(
                    [sys.executable, "-m", "locki", "internal", "daemon"],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            for _ in range(100):  # up to 10s for the daemon to write its port
                with contextlib.suppress(OSError, ValueError):
                    if ssh_port := int(PORT_FILE.read_text().strip()):
                        break
                time.sleep(0.1)
        if not ssh_port:
            logger.warning(
                "Locki daemon did not report a port in time. Command bridge proxy is disabled in this sandbox."
            )
        (client_ssh_dir / "locki-ssh-config").write_text(
            (PACKAGE_DATA / "locki-ssh-config").read_text() + f"    Port {ssh_port}\n    User {getpass.getuser()}\n"
        )


daemon = DaemonService()
