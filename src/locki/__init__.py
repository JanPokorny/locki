import contextlib
import fcntl
import functools
import importlib.resources
import json
import logging
import os
import pathlib
import secrets
import shlex
import shutil
import socket
import string
import subprocess
import sys
import textwrap
import time
import typing

import anyio.to_thread
import typer
from halo import Halo

from locki.async_typer import AsyncTyperWithAliases
from locki.config import load_config
from locki.utils import run_command, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

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


def ensure_mcp_server() -> None:  # TODO: Non-functional at the moment
    """Start the locki MCP server as a host daemon if not already running."""
    pid_file = LOCKI_HOME / "mcp.pid"

    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            return  # already running
        except ProcessLookupError, ValueError, OSError:
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
    """If cwd is inside a locki-managed worktree, return its path."""
    cwd = pathlib.Path.cwd().resolve()
    if not cwd.is_relative_to(WORKTREES_HOME.resolve()):
        return None
    return WORKTREES_HOME / cwd.relative_to(WORKTREES_HOME).parts[0]


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


@app.command(
    "shell",
    help="Open a shell in the per-branch container (creates branch/worktree/container if needed).",
    context_settings={"allow_extra_args": True},
)
async def shell_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to work on (optional if inside a worktree)")
    ] = None,
    command: typing.Annotated[
        str | None, typer.Option("-c", help="Command to run instead of an interactive shell")
    ] = None,
):
    git_root()  # fail fast if not in a git repo

    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    claude_json = CLAUDE_HOME / "claude.json"
    existing = json.loads(claude_json.read_text()) if claude_json.exists() else {}
    existing["projects"] = existing.get("projects", {})
    existing["projects"]["/"] = existing.get("projects", {}).get("/", {})
    existing["projects"]["/"]["hasTrustDialogAccepted"] = True
    claude_json.write_text(json.dumps(existing, indent=2) + "\n")

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

    if branch:
        wt_path = await find_worktree_for_branch(branch)
        if not wt_path:
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

            meta_dir = WORKTREES_META / wt_id
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / ".git").write_text((wt_path / ".git").read_text())

            await run_command(
                ["git", "-C", str(git_root()), "config", "extensions.worktreeConfig", "true"],
                "Enabling per-worktree git config",
            )

            hooks_dir = WORKTREES_META / wt_id / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            hook_script = (importlib.resources.files("locki") / "data" / "locki-hook.sh").read_bytes()
            for name in GIT_HOOKS:
                hook_path = hooks_dir / name
                hook_path.write_bytes(hook_script)
                hook_path.chmod(0o755)

            await run_command(
                ["git", "-C", str(wt_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
                "Configuring per-worktree hooks",
            )
    else:
        wt_path = current_worktree()
        if wt_path is None:
            logger.error("No branch specified and not inside a locki worktree.")
            sys.exit(1)
    wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]

    config = load_config(git_root())

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
    else:
        incus_image = config.get_incus_image()

        local_path = git_root() / incus_image
        if local_path.is_file():
            local_file = local_path.resolve()
            tmp_name = f"locki-img-{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
            await run_command(
                [limactl(), "copy", str(local_file), f"locki:/tmp/{tmp_name}"],
                "Copying image into VM",
                env={"LIMA_HOME": str(LIMA_HOME)},
                cwd="/",
            )
            await run_in_vm(
                ["bash", "-c", f"incus image import /tmp/{tmp_name} --alias={tmp_name} && rm -f /tmp/{tmp_name}"],
                "Importing container image",
            )
            image_ref = tmp_name
        else:
            image_ref = incus_image

        await run_in_vm(
            ["incus", "init", image_ref, wt_id],
            "Creating container",
        )

        if local_path.is_file():
            await run_in_vm(
                ["incus", "image", "delete", image_ref],
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

        stub = (importlib.resources.files("locki") / "data" / "stub.sh").read_text()
        container_files = {
            "/etc/claude-code/CLAUDE.md": (importlib.resources.files("locki") / "data" / "CLAUDE.md").read_text(),
            "/etc/claude-code/managed-mcp.json": json.dumps(
                {
                    "mcpServers": {"locki": {"type": "http", "url": "http://host.lima.internal:7890/mcp"}},
                }
            ),
            "/etc/claude-code/managed-settings.json": json.dumps(
                {
                    "skipDangerousModePermissionPrompt": True,
                    "allowManagedMcpServersOnly": False,
                    "permissions": {"defaultMode": "bypassPermissions"},
                }
            ),
            "/opt/locki/bin/git": stub,
            "/opt/locki/bin/gh": stub,
        }
        for path, content in container_files.items():
            await run_in_vm(
                ["incus", "exec", wt_id, "--", "bash", "-c", f"mkdir -p $(dirname {path}) && cat >{path}"],
                f"Writing {pathlib.PurePosixPath(path).name}",
                input=content.encode(),
            )

        host_ip = (
            await run_in_vm(
                ["bash", "-c", "getent hosts host.lima.internal | awk '{print $1}' | head -1"],
                "Resolving host IP",
                check=False,
            )
        ).stdout.decode().strip()

        await run_in_vm(
            [
                "incus",
                "exec",
                wt_id,
                "--",
                "bash",
                "-euxo",
                "pipefail",
                "-c",
                textwrap.dedent(f"""\
                    for bin in /opt/locki/bin/*; do chmod +x "$bin"; done
                    ln -sf /root/.claude/claude.json /root/.claude.json
                    command -v mise || curl -fsSL https://mise.run | sh || true
                    mkdir -p /etc/dnf && echo -e "cachedir=/var/cache/locki/dnf\\nkeepcache=1" >> /etc/dnf/dnf.conf || true
                    mkdir -p /etc/apt/apt.conf.d && printf 'Dir::Cache "/var/cache/locki/apt/cache";\\nDir::State "/var/cache/locki/apt/state";\\n' > /etc/apt/apt.conf.d/99local-cache || true
                    echo '{host_ip} host.lima.internal' >> /etc/hosts
                """),
            ],
            "Configuring container environment",
        )

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
            "--workdir=/",
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
                    "--rcfile",
                    shlex.quote("<(mise activate bash)"),
                ]
                + (["-c", shlex.quote(command)] if command else [])
                + (["--", *(shlex.quote(a) for a in ctx.args)] if ctx.args else [])
            ),
        ],
    )


@app.command(
    "claude",
    context_settings={"allow_extra_args": True},
)
async def claude_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to work on (optional if inside a worktree)")
    ] = None,
):
    """Run Claude in the sandbox."""
    await shell_cmd(
        ctx=ctx, branch=branch, command='mise use --cd / -g nodejs@24 npm:@anthropic-ai/claude-code@latest && exec claude "$@"'
    )


@app.command("remove", help="Remove a branch's worktree and container.")
async def remove_cmd(
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to remove (optional if inside a worktree)")
    ] = None,
    force: typing.Annotated[bool, typer.Option("--force", "-f", help="Skip safety checks")] = False,
    delete_branch: typing.Annotated[bool, typer.Option("--branch", "-b", help="Also delete the branch")] = False,
):
    if branch:
        wt_path = await find_worktree_for_branch(branch)
    else:
        wt_path = current_worktree()
        if wt_path is None:
            logger.error("No branch specified and not inside a locki worktree.")
            sys.exit(1)

    if wt_path is None:
        logger.info("No locki-managed worktree found for '%s', nothing to do.", branch)
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
        logger.error(
            "Worktree for %s in %s has uncommitted changes. Commit or stash them, or use --force.",
            branch, wt_path,
        )
        sys.exit(1)

    if delete_branch and not branch:
        result = await run_command(
            ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            "Resolving branch name",
            check=False,
        )
        branch = result.stdout.decode().strip() if result.returncode == 0 else None

    wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]

    await run_in_vm(
        ["incus", "delete", "--force", wt_id],
        "Deleting container",
        check=False,
    )

    shutil.rmtree(wt_path, ignore_errors=True)
    shutil.rmtree(WORKTREES_META / wt_id, ignore_errors=True)
    await run_command(
        ["git", "-C", str(git_root()), "worktree", "prune"],
        "Removing worktree",
        check=False,
    )

    if delete_branch:
        await run_command(
            ["git", "-C", str(git_root()), "branch", "-D", branch],
            f"Deleting branch {branch}",
            check=False,
        )


@app.command("list", help="List branches with locki-managed worktrees.")
async def list_cmd():
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
                typer.echo(f"{current_branch}  {current_path}")
                found = True

    if not found:
        logger.info("No locki-managed worktrees found.")


@app.command("factory-reset", help="Delete the locki VM entirely.")
async def factory_reset_cmd():
    await run_command(
        [limactl(), "delete", "-f", "locki"],
        "Deleting VM",
        env={"LIMA_HOME": str(LIMA_HOME)},
        cwd="/",
    )
