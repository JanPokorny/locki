import logging
import os
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import anyio
import anyio.abc
from anyio import create_task_group
from anyio.abc import ByteReceiveStream
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


async def run_command(
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

    async def recv(stream: ByteReceiveStream, buf: BytesIO):
        async for chunk in stream:
            logger.debug("%s", chunk.decode(errors="replace").rstrip())
            buf.write(chunk)

    try:
        start_time = time.time()
        async with await anyio.open_process(
            command,
            stdin=subprocess.PIPE if input else subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
            cwd=cwd,
        ) as proc:
            stdout_buf, stderr_buf = BytesIO(), BytesIO()
            async with create_task_group() as tg:
                if proc.stdout:
                    tg.start_soon(recv, proc.stdout, stdout_buf)
                if proc.stderr:
                    tg.start_soon(recv, proc.stderr, stderr_buf)
                if proc.stdin and input:
                    await proc.stdin.send(input)
                    await proc.stdin.aclose()
            await proc.wait()

            if check and proc.returncode != 0:
                raise subprocess.CalledProcessError(
                    proc.returncode or 0, command, stdout_buf.getvalue(), stderr_buf.getvalue()
                )

            elapsed = int(time.time() - start_time)
            duration = (
                "" if elapsed < 5 else f" ({elapsed}s)" if elapsed < 60 else f" ({elapsed // 60}m{elapsed % 60}s)"
            )
            spinner.succeed(f"{message}{duration}")
            return subprocess.CompletedProcess(
                command, proc.returncode or 0, stdout_buf.getvalue(), stderr_buf.getvalue()
            )
    except FileNotFoundError:
        spinner.fail(message)
        logger.error("%s is not installed. Please install it first.", command[0])
        sys.exit(1)
    except subprocess.CalledProcessError:
        spinner.fail(f"{message} (see {LOG_DIR / 'latest.log'})")
        raise
