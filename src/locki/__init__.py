import contextlib
import fcntl
import functools
import importlib.resources
import os
import pathlib
import re
import secrets
import shlex
import shutil
import socket
import string
import subprocess
import sys
import time
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
WORKTREES_META = LOCKI_HOME / "worktrees-meta"
CLAUDE_HOME = LOCKI_HOME / "claude"
MCP_PORT = 7890


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


@contextlib.asynccontextmanager
async def _vm_lock():
    """Acquire an exclusive file lock so only one process creates/starts the VM at a time."""
    LOCKI_HOME.mkdir(exist_ok=True)
    lock_path = LOCKI_HOME / "vm.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


async def ensure_vm() -> None:
    LOCKI_HOME.mkdir(exist_ok=True)
    LIMA_HOME.mkdir(exist_ok=True, parents=True)
    WORKTREES_HOME.mkdir(parents=True, exist_ok=True)
    async with _vm_lock():
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


def ensure_mcp_server() -> None:
    """Start the locki MCP server as a host daemon if not already running."""
    pid_file = LOCKI_HOME / "mcp.pid"

    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            return  # already running
        except (ProcessLookupError, ValueError, OSError):
            pid_file.unlink(missing_ok=True)

    log_path = LOCKI_HOME / "mcp.log"
    with open(log_path, "a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "locki.mcp_server"],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid))

    # Wait up to 5 s for the server to accept connections
    for _ in range(10):
        try:
            with socket.create_connection(("localhost", MCP_PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.5)


@functools.cache
def git_root() -> pathlib.Path:
    cwd = pathlib.Path.cwd().resolve()
    if cwd.is_relative_to(WORKTREES_HOME.resolve()):
        console.error("locki commands must be run from the main repo checkout, not inside a locki worktree.")
        sys.exit(1)
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.error("Not inside a git repository.")
        sys.exit(1)
    return pathlib.Path(result.stdout.strip())


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
    wt_id = f"{repo_name}--{safe_branch}--{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
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

    # Record the canonical .git pointer outside the VM-visible mount for tamper detection.
    meta_dir = WORKTREES_META / wt_id
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / ".git").write_text((wt_path / ".git").read_text())

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

    # Inject host.lima.internal so containers can reach the MCP server on the host.
    # Lima sets this hostname in the VM's /etc/hosts; containers don't inherit it.
    host_ip_result = await run_in_vm(
        ["bash", "-c", "getent hosts host.lima.internal | awk '{print $1}' | head -1"],
        "Resolving host IP",
        check=False,
    )
    host_ip = host_ip_result.stdout.decode().strip()
    if host_ip:
        await run_in_vm(
            ["incus", "exec", wt_id, "--",
             "bash", "-c", f"echo '{host_ip} host.lima.internal' >> /etc/hosts"],
            "Configuring host access in container",
        )



def git_hooks_dir() -> pathlib.Path:
    return git_root() / ".git" / "hooks"


def find_unwrapped_hooks() -> list[pathlib.Path]:
    """Find executable shell hook files in .git/hooks that haven't been wrapped by locki."""
    hooks_dir = git_hooks_dir()
    if not hooks_dir.is_dir():
        return []
    shell_shebang = re.compile(rb"^#!\s*(?:/usr)?/bin/(?:env\s+)?(?:ba)?sh\b")
    unwrapped = []
    for f in sorted(hooks_dir.iterdir()):
        if f.name.endswith((".sample", ".locki-wrapped")):
            continue
        if not f.is_file() or not os.access(f, os.X_OK):
            continue
        if (hooks_dir / f"{f.name}.locki-wrapped").exists():
            continue
        try:
            with open(f, "rb") as fh:
                if not shell_shebang.match(fh.readline(128)):
                    continue
        except OSError:
            continue
        unwrapped.append(f)
    return unwrapped


@app.command("shell", help="Open a shell in the per-branch container (creates branch/worktree/container if needed).")
async def shell_cmd(
    branch: typing.Annotated[str, typer.Argument(help="Branch name to work on")],
    command: typing.Annotated[
        str | None, typer.Option("-c", help="Command to run instead of an interactive shell")
    ] = None,
    verbose: typing.Annotated[bool, typer.Option("-v", "--verbose", help="Show verbose output")] = False,
):
    with verbosity(verbose):
        git_root()  # fail fast if not in a git repo

        do_not_wrap = git_root() / ".git" / "locki" / "do-not-wrap-hooks"
        if command is None and sys.stdin.isatty() and not do_not_wrap.exists():
            unwrapped = find_unwrapped_hooks()
            if unwrapped:
                from InquirerPy import inquirer

                hook_names = ", ".join(h.name for h in unwrapped)
                if inquirer.confirm(
                    message=f"Found unwrapped git hooks: {hook_names}. Wrap them for Locki?",
                    default=True,
                ).execute():
                    await wrap_git_hooks_cmd()
                else:
                    do_not_wrap.parent.mkdir(parents=True, exist_ok=True)
                    do_not_wrap.touch()

        ensure_mcp_server()
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
                + (["-c", shlex.quote(command)] if command else [])
            ),
        ],
    )


@app.command("claude")
async def claude_cmd(
    branch: typing.Annotated[str, typer.Argument(help="Branch name to work on")],
    verbose: typing.Annotated[bool, typer.Option("-v", "--verbose", help="Show verbose output")] = False,
):
    """Run Claude in the sandbox."""
    await shell_cmd(branch=branch, command="claude", verbose=verbose)


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

        if (
            not force
            and (
                await run_command(
                    ["git", "-C", str(wt_path), "status", "--porcelain"],
                    "Checking for uncommitted changes",
                    check=False,
                )
            ).stdout.strip()
        ):
            console.error(
                f"Worktree for {branch} in {wt_path} has uncommitted changes. Commit or stash them, or use --force."
            )
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

        shutil.rmtree(WORKTREES_META / wt_id, ignore_errors=True)


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


@app.command("wrap-git-hooks", help="Wrap git hooks to run inside the locki sandbox for managed worktrees.")
async def wrap_git_hooks_cmd(
    undo: typing.Annotated[bool, typer.Option("--undo", help="Undo the hook wrapping")] = False,
):
    git_root()  # fail fast if not in a git repo

    hooks_dir = git_hooks_dir()

    if undo:
        if not hooks_dir.is_dir():
            console.info("No locki-wrapped hooks found.")
            return
        hooks = [
            hooks_dir / f.name.removesuffix(".locki-wrapped")
            for f in sorted(hooks_dir.iterdir())
            if f.name.endswith(".locki-wrapped") and (hooks_dir / f.name.removesuffix(".locki-wrapped")).exists()
        ]
        if not hooks:
            console.info("No locki-wrapped hooks found.")
            return
        for hook in hooks:
            hook.unlink()
            (hook.parent / f"{hook.name}.locki-wrapped").rename(hook)
            console.print(f"Unwrapped {hook.name}")
        console.info(f"Restored {len(hooks)} hook(s) to their original state.")
    else:
        hooks = find_unwrapped_hooks()
        if not hooks:
            console.info("No hooks to wrap (all hooks are already wrapped or none exist).")
            return
        wrapper_src = importlib.resources.files("locki") / "data" / "hook-wrapper.sh"
        for hook in hooks:
            hook.rename(hook.parent / f"{hook.name}.locki-wrapped")
            shutil.copy2(wrapper_src, hook)
            hook.chmod(0o755)
            console.print(f"Wrapped {hook.name}")
        console.info(f"Wrapped {len(hooks)} hook(s). Use 'locki wrap-git-hooks --undo' to reverse.")


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
