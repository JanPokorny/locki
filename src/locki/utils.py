import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from halo import Halo

LOG_DIR = Path.home() / ".locki" / "logs"

logger = logging.getLogger(__name__)


class _StderrFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno >= logging.ERROR:
            return f"ERROR: {record.getMessage()}"
        return record.getMessage()


def setup_logging():
    root = logging.getLogger("locki")
    root.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(_StderrFormatter())
    root.addHandler(stderr_handler)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_DIR / "latest.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(file_handler)


def run_command(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    cwd: str = ".",
    check: bool = True,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    logger.debug("Command: %s", command)
    spinner = Halo(text=message, spinner="dots", stream=sys.stderr)
    spinner.start()

    try:
        start_time = time.time()
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL if input is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
            cwd=cwd,
            input=input,
        )
        if result.stdout:
            logger.debug("%s", result.stdout.decode(errors="replace").rstrip())
        if result.stderr:
            logger.debug("%s", result.stderr.decode(errors="replace").rstrip())

        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, command, result.stdout, result.stderr
            )

        elapsed = int(time.time() - start_time)
        duration = (
            "" if elapsed < 5 else f" ({elapsed}s)" if elapsed < 60 else f" ({elapsed // 60}m{elapsed % 60}s)"
        )
        spinner.succeed(f"{message}{duration}")
        return result
    except FileNotFoundError:
        spinner.fail(message)
        logger.error("%s is not installed. Please install it first.", command[0])
        sys.exit(1)
    except subprocess.CalledProcessError:
        spinner.fail(f"{message} (see {LOG_DIR / 'latest.log'})")
        raise
