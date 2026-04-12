import datetime
import logging
import os
import random
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path

LOG_DIR = Path.home() / ".locki" / "logs"


class Spinner:
    """Minimal terminal spinner: random Norse runes on stderr."""

    def __init__(self, text: str):
        self._text = text
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._stop.clear()

        def spin():
            while not self._stop.wait(0.2):
                sys.stderr.write(f"\r{random.choice('ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛋᛏᛒᛖᛗᛚᛜᛝᛟᛞ')} {self._text}")
                sys.stderr.flush()

        self._thread = threading.Thread(target=spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        sys.stderr.write("\r\033[2K")
        sys.stderr.flush()

    def succeed(self, text: str):
        self.__exit__()
        sys.stderr.write(f"\r\033[32m\u2714\033[0m {text}\n")
        sys.stderr.flush()

    def fail(self, text: str):
        self.__exit__()
        sys.stderr.write(f"\r\033[31m\u2716\033[0m {text}\n")
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
    spinner = None
    with (spinner := Spinner(message)) if not quiet else nullcontext():
        try:
            start_time = time.time()
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

            elapsed = int(time.time() - start_time)
            duration = "" if elapsed < 5 else f" ({elapsed}s)" if elapsed < 60 else f" ({elapsed // 60}m{elapsed % 60}s)"
            if spinner:
                spinner.succeed(f"{message}{duration}")
            return result
        except FileNotFoundError:
            if spinner:
                spinner.fail(message)
            logger.error("%s is not installed. Please install it first.", command[0])
            sys.exit(1)
        except subprocess.CalledProcessError:
            if spinner:
                spinner.fail(message)
            _print_log_tail()
            raise
