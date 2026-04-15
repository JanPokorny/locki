import datetime
import logging
import os
import pathlib
import sys

import click

from locki.config import LOG_DIR

_log_file_path: pathlib.Path | None = None


class _StderrFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno >= logging.ERROR:
            return f"{click.style('ERROR', fg='red')}: {record.getMessage()}"
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


def print_log_tail():
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
