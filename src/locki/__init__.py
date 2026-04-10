import contextlib
import fcntl
import functools
import importlib.resources
import logging
import os
import pathlib
import shutil
import subprocess
import sys

import click
from halo import Halo

from locki.utils import run_command, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

LOCKI_HOME = pathlib.Path.home() / ".locki"
LIMA_HOME = LOCKI_HOME / "lima"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
WORKTREES_META = LOCKI_HOME / "worktrees-meta"

GIT_HOOKS = [
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-commit",
    "pre-merge-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-push",
    "pre-receive",
    "update",
    "proc-receive",
    "post-receive",
    "post-update",
    "reference-transaction",
    "push-to-checkout",
    "pre-auto-gc",
    "post-rewrite",
    "sendemail-validate",
    "fsmonitor-watchman",
]


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


@click.group(cls=AliasGroup, help="AI sandboxing without the taste of sand, using a managed Lima VM with Incus containers.")
def app():
    pass


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


@contextlib.contextmanager
def _file_lock(name: str, wait_message: str):
    """Acquire an exclusive file lock."""
    LOCKI_HOME.mkdir(exist_ok=True)
    lock_path = LOCKI_HOME / f"{name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            with Halo(text=wait_message, spinner="dots", stream=sys.stderr):
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


# Register commands (imported here to avoid circular imports)
from locki.port_forward import port_forward_cmd  # noqa: E402
from locki.safe_cmd import safe_cmd  # noqa: E402
from locki.shell import claude_cmd, codex_cmd, gemini_cmd, opencode_cmd, shell_cmd  # noqa: E402
from locki.vm import vm_app  # noqa: E402
from locki.worktree import remove_cmd, status_cmd, stop_cmd  # noqa: E402

app.add_command(shell_cmd, "shell | sh | bash")
app.add_command(claude_cmd, "claude")
app.add_command(gemini_cmd, "gemini")
app.add_command(codex_cmd, "codex")
app.add_command(opencode_cmd, "opencode")
app.add_command(port_forward_cmd, "port-forward | pf")
app.add_command(remove_cmd, "remove | rm | delete")
app.add_command(stop_cmd, "stop")
app.add_command(status_cmd, "status | st")
app.add_command(safe_cmd, "safe-cmd")
app.add_command(vm_app, "vm")
