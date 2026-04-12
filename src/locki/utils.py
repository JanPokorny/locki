import datetime
import logging
import os
import random
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

LOG_DIR = Path.home() / ".locki" / "logs"


@contextmanager
def spinner(text: str):
    stop = threading.Event()
    start = time.time()

    def _spin():
        while not stop.wait(0.2):
            sys.stderr.write(f"\r{random.choice('ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛋᛏᛒᛖᛗᛚᛜᛝᛟᛞ')} {text}")
            sys.stderr.flush()

    def _duration() -> str:
        elapsed = int(time.time() - start)
        if elapsed < 5:
            return ""
        s = f" ({elapsed}s)" if elapsed < 60 else f" ({elapsed // 60}m{elapsed % 60}s)"
        return f"\033[2m{s}\033[0m"

    thread = threading.Thread(target=_spin, daemon=True)
    thread.start()
    try:
        yield
        stop.set()
        thread.join()
        sys.stderr.write(f"\r\033[2K\033[32m\u2714\033[0m {text}{_duration()}\n")
    except BaseException:
        stop.set()
        thread.join()
        sys.stderr.write(f"\r\033[2K\033[31m\u2716\033[0m {text}{_duration()}\n")
        raise
    finally:
        sys.stderr.flush()


logger = logging.getLogger(__name__)

_log_file_path: Path | None = None


class _StderrFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno >= logging.ERROR:
            return f"ERROR: {record.getMessage()}"
        return record.getMessage()


def setup_logging():
    global _log_file_path

    root = logging.getLogger("locki")
    root.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(_StderrFormatter())
    root.addHandler(stderr_handler)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    _log_file_path = LOG_DIR / f"{timestamp}-{os.getpid()}.log"
    file_handler = logging.FileHandler(_log_file_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(file_handler)

    # Clean up old log files, keep the 20 most recent
    log_files = sorted(LOG_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old_log in log_files[20:]:
        old_log.unlink(missing_ok=True)


def _print_log_tail():
    if not _log_file_path or not _log_file_path.exists():
        return
    try:
        lines = _log_file_path.read_text().splitlines()
        tail = lines[-10:]
        if tail:
            print(f"\nRecent log entries ({_log_file_path}):", file=sys.stderr)
            for line in tail:
                print(f"  {line}", file=sys.stderr)
    except OSError:
        pass


def run_command(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    cwd: str = ".",
    check: bool = True,
    input: bytes | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    logger.debug("Command: %s", command)
    with spinner(message) if not quiet else nullcontext():
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                env={**os.environ, **(env or {})},
                cwd=cwd,
                input=input,
            )
            logger.debug("%s", result.stdout.decode(errors="replace").rstrip())
            logger.debug("%s", result.stderr.decode(errors="replace").rstrip())

            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)

            return result
        except FileNotFoundError:
            logger.error("%s is not installed. Please install it first.", command[0])
            sys.exit(1)
        except subprocess.CalledProcessError:
            _print_log_tail()
            raise
