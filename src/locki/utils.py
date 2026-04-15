import fcntl
import functools
import importlib.resources
import logging
import os
import pathlib
import random
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext

import click

from locki.config import LIMA_HOME, LOCKI_HOME, WORKTREES_HOME, WORKTREES_META
from locki.logging import print_log_tail

logger = logging.getLogger(__name__)


class AliasGroup(click.Group):
    """Click group that supports pipe-separated command aliases (e.g. 'shell | sh | bash')."""

    def get_command(self, ctx, cmd_name):
        # Direct match first
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        # Try alias match
        for name in self.list_commands(ctx):
            if cmd_name in name.split(" | "):
                return super().get_command(ctx, name)
        return None

    def format_commands(self, ctx, formatter):
        """Write the commands, showing only the primary name."""
        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            primary = subcommand.split(" | ")[0]
            help_text = cmd.get_short_help_str(limit=formatter.width)
            commands.append((primary, help_text))
        if commands:
            with formatter.section("Commands"):
                formatter.write_dl(commands)


@contextmanager
def spinner(text: str):
    stop = threading.Event()
    start = time.time()

    def _spin():
        while not stop.wait(0.2):
            sys.stderr.write(f"\r{random.choice('ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛋᛏᛒᛖᛗᛚᛜᛝᛟᛞᚴ')} {text}")
            sys.stderr.flush()

    def _duration() -> str:
        elapsed = int(time.time() - start)
        if elapsed < 5:
            return ""
        s = f" ({elapsed}s)" if elapsed < 60 else f" ({elapsed // 60}m{elapsed % 60}s)"
        return click.style(s, dim=True)

    thread = threading.Thread(target=_spin, daemon=True)
    thread.start()
    try:
        yield
        stop.set()
        thread.join()
        click.echo(f"\r{click.style("ᛝ", fg="green", bold=True)} {text.replace("ing ", "ed ", count=1)}{_duration()} ", err=True)
    except BaseException:
        stop.set()
        thread.join()
        click.echo(f"\r{click.style("ᛞ", fg="red", bold=True)} {text} failed{_duration()}", err=True)
        raise
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
            print_log_tail()
            raise


@functools.cache
def limactl() -> str:
    bundled = importlib.resources.files("locki") / "data" / "bin" / "limactl"
    if bundled.is_file():
        return str(bundled)
    system = shutil.which("limactl")
    if system:
        return system
    logger.error("limactl is not installed. Please install Lima or use a platform-specific locki wheel.")
    sys.exit(1)


def run_in_vm(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    return run_command(
        [limactl(), "shell", "--start", "--preserve-env", "--tty=false", "locki", "--", "sudo", "-E", *command],
        message,
        env={"LIMA_HOME": str(LIMA_HOME)} | (env or {}),
        cwd="/",
        input=input,
        check=check,
        quiet=quiet,
    )


@contextmanager
def file_lock(name: str, wait_message: str):
    """Acquire an exclusive file lock."""
    LOCKI_HOME.mkdir(exist_ok=True)
    lock_path = LOCKI_HOME / f"{name}.lock"
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


@functools.cache
def git_root() -> pathlib.Path:
    cwd = pathlib.Path.cwd().resolve()
    if cwd.is_relative_to(WORKTREES_HOME.resolve()):
        wt_path = WORKTREES_HOME / cwd.relative_to(WORKTREES_HOME).parts[0]
        meta_git = WORKTREES_META / wt_path.name / ".git"
        if not meta_git.exists():
            logger.error("No worktree metadata found for '%s'.", wt_path.name)
            sys.exit(1)
        (wt_path / ".git").write_text(meta_git.read_text())
        result = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("Could not determine main repo from worktree metadata.")
            sys.exit(1)
        return pathlib.Path(result.stdout.strip()).parent
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Not inside a git repository.")
        sys.exit(1)
    return pathlib.Path(result.stdout.strip())


def current_worktree() -> pathlib.Path | None:
    """If cwd is inside a Locki-managed worktree, return its path."""
    cwd = pathlib.Path.cwd().resolve()
    if not cwd.is_relative_to(WORKTREES_HOME.resolve()):
        return None
    return WORKTREES_HOME / cwd.relative_to(WORKTREES_HOME).parts[0]


def resolve_branch(branch: str | None) -> tuple[str, pathlib.Path]:
    """Resolve a branch name to (branch, worktree_path). Errors if unresolvable."""
    if branch:
        wt_path = find_worktree_for_branch(branch)
        if wt_path is None:
            logger.error("No worktree found for branch '%s'.", branch)
            sys.exit(1)
        return branch, wt_path
    wt_path = current_worktree()
    if wt_path is None:
        logger.error("No branch specified and not inside a locki worktree.")
        sys.exit(1)
    return wt_path.relative_to(WORKTREES_HOME).parts[0], wt_path


def find_worktree_for_branch(branch: str) -> pathlib.Path | None:
    """Return the worktree path for a branch managed by Locki, or None."""
    result = run_command(
        ["git", "-C", str(git_root()), "worktree", "list", "--porcelain"],
        "Listing worktrees",
    )
    current_path: pathlib.Path | None = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1])
        elif (
            line.startswith("branch refs/heads/")
            and line.removeprefix("branch refs/heads/") == branch
            and current_path
            and current_path.is_relative_to(WORKTREES_HOME)
        ):
            return current_path
    return None


def list_locki_worktree_branches() -> list[str]:
    """Return branch names that have Locki-managed worktrees in the current repo."""
    result = subprocess.run(
        ["git", "-C", str(git_root()), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    branches: list[str] = []
    current_path: pathlib.Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1])
        elif (
            line.startswith("branch refs/heads/")
            and current_path
            and current_path.is_relative_to(WORKTREES_HOME)
        ):
            branches.append(line.removeprefix("branch refs/heads/"))
    return branches


def list_local_branches() -> list[str]:
    """Return all local branch names."""
    result = subprocess.run(
        ["git", "-C", str(git_root()), "branch", "--format=%(refname:short)"],
        capture_output=True, text=True,
    )
    return [b.strip() for b in result.stdout.splitlines() if b.strip()]


def list_remote_branches() -> list[str]:
    """Return remote branch names with the remote prefix stripped."""
    result = subprocess.run(
        ["git", "-C", str(git_root()), "branch", "-r", "--format=%(refname:short)"],
        capture_output=True, text=True,
    )
    seen: set[str] = set()
    branches: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        _, _, branch = line.partition("/")
        if branch and branch != "HEAD" and branch not in seen:
            seen.add(branch)
            branches.append(branch)
    return branches
