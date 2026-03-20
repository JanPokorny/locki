import functools
import importlib.resources
import os
import pathlib
import secrets
import shlex
import shutil
import subprocess
import sys
import textwrap
import typing

import typer

from locki.async_typer import AsyncTyperWithAliases
from locki.config import load_config
from locki.console import console
from locki.utils import run_command, verbosity

app = AsyncTyperWithAliases(
    name="locki",
    help="Lima VM wrapper that protects worktrees by offering isolated execution environments.",
    no_args_is_help=True,
)

LOCKI_HOME = pathlib.Path.home() / ".locki"
LIMA_HOME = LOCKI_HOME / "lima"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
CLAUDE_HOME = LOCKI_HOME / "claude"


@functools.cache
def limactl() -> str:
    bundled = importlib.resources.files("locki") / "data" / "bin" / "limactl"
    if bundled.is_file():
        return str(bundled)
    system = shutil.which("limactl")
    if system:
        return system
    console.error("limactl is not installed. Please install Lima or use a platform-specific locki wheel.")
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


async def ensure_vm() -> None:
    LOCKI_HOME.mkdir(exist_ok=True)
    LIMA_HOME.mkdir(exist_ok=True, parents=True)
    WORKTREES_HOME.mkdir(parents=True, exist_ok=True)
    await run_command(
        [
            limactl(),
            "--tty=false",
            "create",
            str(importlib.resources.files("locki").joinpath("data/locki.yaml")),
            "--mount-writable",
            "--name=locki",
        ],
        "Preparing VM",
        env={"LIMA_HOME": str(LIMA_HOME)},
        cwd="/",
        check=False,
    )
    await run_command(
        [
            limactl(),
            "--tty=false",
            "start",
            "locki",
        ],
        "Starting VM",
        env={"LIMA_HOME": str(LIMA_HOME)},
        cwd="/",
        check=False,
    )


def ensure_claude_data() -> None:
    """Seed ~/.locki/claude with bundled config files if they don't already exist."""
    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    data = importlib.resources.files("locki") / "data"
    for name in ["settings.json", "CLAUDE.md", "claude.json"]:
        dst = CLAUDE_HOME / name
        if not dst.exists():
            dst.write_text((data / name).read_text())


@functools.cache
def git_root() -> pathlib.Path:
    current = pathlib.Path.cwd()
    while True:
        dot_git = current / ".git"
        if dot_git.is_dir():
            return current
        if dot_git.is_file():
            content = dot_git.read_text().strip()
            if content.startswith("gitdir:"):
                wt_gitdir = pathlib.Path(content.split(":", 1)[1].strip())
                if not wt_gitdir.is_absolute():
                    wt_gitdir = (current / wt_gitdir).resolve()
                main_git_dir = (wt_gitdir / ".." / "..").resolve()
                if main_git_dir.name == ".git":
                    return main_git_dir.parent
            return current
        if current.parent == current:
            console.error("Not inside a git repository.")
            sys.exit(1)
        current = current.parent


async def find_worktree_for_branch(branch: str) -> pathlib.Path | None:
    """Return the worktree path for a branch managed by locki, or None."""
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


async def ensure_worktree(branch: str) -> pathlib.Path:
    """Ensure a locki-managed worktree exists for the branch. Returns the worktree path."""
    existing = await find_worktree_for_branch(branch)
    if existing:
        return existing

    await run_command(
        ["git", "-C", str(git_root()), "worktree", "prune"],
        "Pruning stale git worktrees",
    )

    repo_name = git_root().name.replace("/", "-").replace(".", "-").lower()
    safe_branch = branch.replace("/", "-").replace(".", "-").lower()
    wt_id = f"{repo_name}--{safe_branch}--{secrets.token_hex(4)}"
    wt_path = WORKTREES_HOME / wt_id
    wt_path.mkdir(parents=True, exist_ok=True)

    result = await run_command(
        ["git", "-C", str(git_root()), "rev-parse", "--verify", f"refs/heads/{branch}"],
        f"Checking if branch '{branch}' exists",
        check=False,
    )
    if result.returncode != 0:
        await run_command(
            ["git", "-C", str(git_root()), "branch", branch],
            f"Creating branch '{branch}'",
        )

    await run_command(
        ["git", "-C", str(git_root()), "worktree", "add", str(wt_path), branch],
        f"Creating worktree for '{branch}'",
    )

    return wt_path


async def ensure_container(wt_id: str, wt_path: pathlib.Path, config) -> None:
    """Ensure an Incus container exists for the given worktree (idempotent)."""
    result = await run_in_vm(
        ["incus", "list", "--format=csv", "--columns=n", wt_id],
        "Checking container",
        check=False,
    )
    if wt_id in result.stdout.decode():
        await run_in_vm(
            ["incus", "start", wt_id],
            "Starting container",
            check=False,
        )
        return

    incus_image = config.get_incus_image()

    local_path = git_root() / incus_image
    if local_path.is_file():
        local_file = local_path.resolve()
        await run_command(
            [limactl(), "copy", str(local_file), "locki:/tmp/image"],
            "Copying image into VM",
            env={"LIMA_HOME": str(LIMA_HOME)},
            cwd="/",
        )
        await run_in_vm(
            ["incus", "image", "import", "/tmp/image", f"--alias={wt_id}"],
            "Importing container image",
        )
        await run_in_vm(["rm", "-f", "/tmp/image"], "Cleaning up image file", check=False)
        image_ref = wt_id
    else:
        image_ref = incus_image

    await run_in_vm(
        ["incus", "init", image_ref, wt_id],
        "Creating container",
    )

    if local_path.is_file():
        await run_in_vm(
            ["incus", "image", "delete", wt_id],
            "Cleaning up imported image",
            check=False,
        )

    await run_in_vm(
        [
            "incus",
            "config",
            "device",
            "add",
            wt_id,
            "worktree",
            "disk",
            f"source={wt_path}",
            f"path={wt_path}",
        ],
        "Mounting worktree into container",
    )

    await run_in_vm(
        ["incus", "start", wt_id],
        "Starting container",
    )


@app.command("shell", help="Open a shell in the per-branch container (creates branch/worktree/container if needed).")
async def shell_cmd(
    branch: typing.Annotated[str, typer.Argument(help="Branch name to work on")],
    verbose: typing.Annotated[bool, typer.Option("-v", "--verbose", help="Show verbose output")] = False,
):
    with verbosity(verbose):
        git_root()  # fail fast if not in a git repo

        ensure_claude_data()
        await ensure_vm()

        wt_path = await ensure_worktree(branch)
        wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]

        config = load_config(git_root())
        await ensure_container(wt_id, wt_path, config)

    forwarded_env = {"TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LANG", "SSH_TTY"}

    os.environ["LIMA_HOME"] = str(LIMA_HOME)
    os.environ["LIMA_SHELLENV_ALLOW"] = ",".join(forwarded_env)

    os.execvp(
        limactl(),
        [
            limactl(),
            "shell",
            "--yes",
            "--preserve-env",
            "--start",
            "locki",
            "--",
            "bash",
            "-c",
            " ".join(
                [
                    "sudo",
                    "incus",
                    "exec",
                    shlex.quote(wt_id),
                    "--cwd",
                    shlex.quote(str(wt_path)),
                    *(f"--env={env}=${env}" for env in forwarded_env),
                    "--",
                    "bash",
                    "--login",
                ]
            ),
        ],
    )


@app.command("remove", help="Remove a branch's worktree and container.")
async def remove_cmd(
    branch: typing.Annotated[str, typer.Argument(help="Branch name to remove")],
    force: typing.Annotated[bool, typer.Option("--force", "-f", help="Skip safety checks")] = False,
    verbose: typing.Annotated[bool, typer.Option("-v", "--verbose", help="Show verbose output")] = False,
):
    with verbosity(verbose):
        wt_path = await find_worktree_for_branch(branch)

        if wt_path is None:
            console.info(f"No locki-managed worktree found for '{branch}', nothing to do.")
            return

        if not force and (await run_command(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            "Checking for uncommitted changes",
            check=False,
        )).stdout.strip():
            console.error(f"Worktree for {branch} in {wt_path} has uncommitted changes. Commit or stash them, or use --force.")
            sys.exit(1)

        wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]

        await run_in_vm(
            ["incus", "delete", "--force", wt_id],
            "Deleting container",
            check=False,
        )

        await run_command(
            ["git", "-C", str(git_root()), "worktree", "remove", "--force", str(wt_path)],
            "Removing worktree",
            check=False,
        )


@app.command("list", help="List branches with locki-managed worktrees.")
async def list_cmd(
    verbose: typing.Annotated[bool, typer.Option("-v", "--verbose", help="Show verbose output")] = False,
):
    with verbosity(verbose, show_success_status=False):
        result = await run_command(
            ["git", "-C", str(git_root()), "worktree", "list", "--porcelain"],
            "Listing worktrees",
        )

    found = False
    current_path: pathlib.Path | None = None
    current_branch: str | None = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1])
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
        elif line == "" and current_path and current_branch:
            if current_path.is_relative_to(WORKTREES_HOME):
                console.print(f"{current_branch}  [dim]{current_path}[/dim]")
                found = True

    if not found:
        console.info("No locki-managed worktrees found.")


@app.command("factory-reset", help="Delete the locki VM entirely.")
async def factory_reset_cmd(
    verbose: typing.Annotated[bool, typer.Option("-v", "--verbose", help="Show verbose output")] = False,
):
    with verbosity(verbose):
        await run_command(
            [limactl(), "delete", "-f", "locki"],
            "Deleting VM",
            env={"LIMA_HOME": str(LIMA_HOME)},
            cwd="/",
        )
