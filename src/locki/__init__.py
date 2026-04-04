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

import anyio.to_thread
from halo import Halo

from locki.async_typer import AsyncTyper
from locki.utils import run_command, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

app = AsyncTyper(
    name="locki",
    help="Lima VM wrapper that protects worktrees by offering isolated execution environments.",
    no_args_is_help=True,
)

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


async def run_in_vm(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return await run_command(
        [limactl(), "shell", "--start", "--preserve-env", "--tty=false", "locki", "--", "sudo", "-E", *command],
        message,
        env={"LIMA_HOME": str(LIMA_HOME)} | (env or {}),
        cwd="/",
        input=input,
        check=check,
    )


@contextlib.asynccontextmanager
async def _vm_lock():
    """Acquire an exclusive file lock so only one process creates/starts the VM at a time."""
    LOCKI_HOME.mkdir(exist_ok=True)
    lock_path = LOCKI_HOME / "vm.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            with Halo(text="Waiting for another locki process", spinner="dots", stream=sys.stderr):
                await anyio.to_thread.run_sync(lambda: fcntl.flock(fd, fcntl.LOCK_EX))
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


async def find_worktree_for_branch(branch: str) -> pathlib.Path | None:
    """Return the worktree path for a branch managed by Locki, or None."""
    result = await run_command(
        ["git", "-C", str(git_root()), "worktree", "list", "--porcelain"],
        "Listing worktrees",
    )
    current_path: pathlib.Path | None = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1])
        elif (
            line.startswith("branch refs/heads/")
            and line.split("/")[-1] == branch
            and current_path
            and current_path.is_relative_to(WORKTREES_HOME)
        ):
            return current_path
    return None


# Register commands — import bare functions, apply decorators here.
from locki.shell import shell_cmd, claude_cmd, gemini_cmd, codex_cmd  # noqa: E402
from locki.worktree import remove_cmd, list_cmd  # noqa: E402
from locki.safe_cmd import safe_cmd  # noqa: E402
from locki.vm import vm_app  # noqa: E402

app.command("shell | sh | bash", help="Open a shell in the per-branch container.",
            context_settings={"allow_extra_args": True})(shell_cmd)
app.command("claude", context_settings={"allow_extra_args": True})(claude_cmd)
app.command("gemini", context_settings={"allow_extra_args": True})(gemini_cmd)
app.command("codex", context_settings={"allow_extra_args": True})(codex_cmd)
app.command("remove | rm | delete", help="Remove a branch's worktree and container.")(remove_cmd)
app.command("list | ls", help="List branches with Locki-managed worktrees.")(list_cmd)
app.command("safe-cmd", hidden=True)(safe_cmd)
app.add_typer(vm_app)
