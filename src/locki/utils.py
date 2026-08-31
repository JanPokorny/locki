import fcntl
import functools
import logging
import os
import pathlib
import random
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext

import click

from locki.logging import print_log_tail
from locki.paths import HOME, RUNTIME
from locki.runes import ERROR, FUTHARK, SUCCESS

logger = logging.getLogger(__name__)

CLEAR_LINE = "\r\033[2K"


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        elif k in result and isinstance(result[k], list) and isinstance(v, list):
            result[k] = result[k] + [x for x in v if x not in result[k]]
        else:
            result[k] = v
    return result


def fail(msg: str):
    click.echo(f"{ERROR} {msg}", err=True)
    sys.exit(1)


class AliasGroup(click.Group):
    """Click group that supports pipe-separated command aliases (e.g. 'shell | sh | bash')."""

    def get_command(self, ctx, cmd_name):
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        for name in self.list_commands(ctx):
            if cmd_name in name.split(" | "):
                return super().get_command(ctx, name)
        return None

    def format_commands(self, ctx, formatter):
        """Write the commands, showing only the primary name."""
        commands = [
            (name.split(" | ")[0], cmd.get_short_help_str(limit=formatter.width))
            for name in self.list_commands(ctx)
            if (cmd := self.get_command(ctx, name)) and not cmd.hidden
        ]
        if commands:
            with formatter.section("Commands"):
                formatter.write_dl(commands)


def sandbox_options(create: bool = False):
    """Shared `-m/-i[/-n]` sandbox-selection options; enforces their mutual exclusivity."""

    def deco(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if sum([bool(kwargs.get("create")), kwargs["match"] is not None, kwargs["interactive"]]) > 1:
                fail("--new, --match, and --interactive are mutually exclusive.")
            return f(*args, **kwargs)

        if create:
            wrapper = click.option("-n", "--new", "create", is_flag=True, default=False, help="Create a new sandbox.")(
                wrapper
            )
        wrapper = click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")(
            wrapper
        )
        return click.option("-m", "--match", default=None, help="Match a sandbox by id prefix or branch substring.")(
            wrapper
        )

    return deco


json_option = click.option("--json", "as_json", is_flag=True, default=False, help="Print the result as JSON to stdout.")


@contextmanager
def spinner(text: str, print_success: bool = True):
    stop = threading.Event()
    start = time.time()

    def _spin():
        while not stop.wait(0.2):
            sys.stderr.write(f"\r{random.choice(FUTHARK)} {text}")
            sys.stderr.flush()

    def _duration() -> str:
        elapsed = int(time.time() - start)
        if elapsed < 5:
            return ""
        s = f" ({elapsed}s)" if elapsed < 60 else f" ({elapsed // 60}m{elapsed % 60}s)"
        return click.style(s, dim=True)

    thread: threading.Thread | None = None
    if sys.stderr.isatty():
        thread = threading.Thread(target=_spin, daemon=True)
        thread.start()
    elif print_success:
        sys.stderr.write(f"\n[spinner] {text}")
        sys.stderr.flush()

    def _stop_spinner():
        if thread:
            stop.set()
            thread.join()

    try:
        yield
    except BaseException:
        _stop_spinner()
        click.echo(f"\r{ERROR} {text} failed{_duration()}", err=True)
        raise
    else:
        _stop_spinner()
        if print_success:
            # ceiling: naive "-ing"→"-ed" grammar; call sites must pick messages that survive it
            click.echo(f"\r{SUCCESS} {text.replace('ing ', 'ed ', 1)}{_duration()} ", err=True)
        elif thread:
            sys.stderr.write(CLEAR_LINE)
    finally:
        sys.stderr.flush()


def run_command(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    cwd: str = ".",
    check: bool = True,
    input: bytes | None = None,
    quiet: bool = False,
    print_success: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    logger.debug("Command: %s", command)
    with spinner(message, print_success=print_success) if not quiet else nullcontext():
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
            fail(f"{command[0]} is not installed. Please install it first.")
        except subprocess.CalledProcessError:
            print_log_tail()
            raise


@contextmanager
def file_lock(name: str, wait_message: str):
    """Acquire an exclusive file lock."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock_path = RUNTIME / f"{name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            with spinner(wait_message):
                fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def pretty_path(p: pathlib.Path) -> str:
    try:
        return "~/" + str(p.relative_to(HOME))
    except ValueError:
        return str(p)


def format_age(timestamp: float | None) -> str:
    """Compact age of a unix timestamp: 'just now', '5m', '3h', '12d', '8w'; '-' when unknown."""
    if timestamp is None:
        return "-"
    seconds = max(0.0, time.time() - timestamp)
    if seconds < 60:
        return "just now"
    for limit, div, unit in ((3600, 60, "m"), (86400, 3600, "h"), (1209600, 86400, "d")):
        if seconds < limit:
            return f"{int(seconds // div)}{unit}"
    return f"{int(seconds // 604800)}w"


def format_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers)]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)
