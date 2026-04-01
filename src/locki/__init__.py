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
from dataclasses import dataclass

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
CODEX_HOME = LOCKI_HOME / "codex"
MCP_PORT = 7890
FORWARDED_ENV = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LANG", "SSH_TTY")

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


def data_file(name: str) -> pathlib.Path:
    return importlib.resources.files("locki").joinpath("data", name)


def data_text(name: str) -> str:
    return data_file(name).read_text()


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    host_state_dir: pathlib.Path
    guest_state_dir: pathlib.PurePosixPath
    launch_argv_builder: typing.Callable[[pathlib.Path, list[str]], list[str]]
    container_file_builder: typing.Callable[[pathlib.Path], dict[str, str]]
    host_state_prep: typing.Callable[[], None] | None = None
    container_setup_builder: typing.Callable[[], str | None] | None = None


def prepare_claude_state() -> None:
    claude_json = CLAUDE_HOME / "claude.json"
    existing = json.loads(claude_json.read_text()) if claude_json.exists() else {}
    existing["projects"] = existing.get("projects", {})
    existing["projects"]["/"] = existing.get("projects", {}).get("/", {})
    existing["projects"]["/"]["hasTrustDialogAccepted"] = True
    claude_json.write_text(json.dumps(existing, indent=2) + "\n")


def build_node_tool_setup(extra_setup: str | None = None) -> str:
    setup = textwrap.dedent(
        """\
        mise use -g node@24
        mise exec node@24 -- npm -v >/dev/null
        """
    )
    if not extra_setup:
        return setup
    return f"{setup}{extra_setup}\n"


def build_claude_launch_argv(_: pathlib.Path, extra_args: list[str]) -> list[str]:
    return ["mise", "exec", "npm:@anthropic-ai/claude-code@latest", "--", "claude", *extra_args]


def build_codex_launch_argv(wt_path: pathlib.Path, extra_args: list[str]) -> list[str]:
    return [
        "env",
        "CODEX_HOME=/root/.codex",
        "mise",
        "exec",
        "npm:@openai/codex@latest",
        "--",
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        'model_instructions_file="/etc/codex/LOCKI.md"',
        "-c",
        f'projects."{wt_path}".trust_level="trusted"',
        *extra_args,
    ]


def build_claude_container_files(_: pathlib.Path) -> dict[str, str]:
    return {
        "/etc/claude-code/CLAUDE.md": data_text("CLAUDE.md"),
        "/etc/claude-code/managed-mcp.json": json.dumps(
            {"mcpServers": {"locki": {"type": "http", "url": "http://host.lima.internal:7890/mcp"}}}
        ),
        "/etc/claude-code/managed-settings.json": json.dumps(
            {
                "skipDangerousModePermissionPrompt": True,
                "allowManagedMcpServersOnly": False,
                "permissions": {"defaultMode": "bypassPermissions"},
            }
        ),
    }


def build_codex_container_files(_: pathlib.Path) -> dict[str, str]:
    return {"/etc/codex/LOCKI.md": data_text("CODEX.md")}


def build_claude_container_setup() -> str | None:
    return build_node_tool_setup("ln -sf /root/.claude/claude.json /root/.claude.json")


def build_codex_container_setup() -> str | None:
    return build_node_tool_setup()


PROVIDERS = {
    "claude": ProviderSpec(
        name="claude",
        host_state_dir=CLAUDE_HOME,
        guest_state_dir=pathlib.PurePosixPath("/root/.claude"),
        launch_argv_builder=build_claude_launch_argv,
        container_file_builder=build_claude_container_files,
        host_state_prep=prepare_claude_state,
        container_setup_builder=build_claude_container_setup,
    ),
    "codex": ProviderSpec(
        name="codex",
        host_state_dir=CODEX_HOME,
        guest_state_dir=pathlib.PurePosixPath("/root/.codex"),
        launch_argv_builder=build_codex_launch_argv,
        container_file_builder=build_codex_container_files,
        container_setup_builder=build_codex_container_setup,
    ),
}


@functools.cache
def limactl() -> str:
    bundled = data_file("bin/limactl")
    if bundled.is_file():
        return str(bundled)
    system = shutil.which("limactl")
    if system:
        return system
    logger.error("limactl is not installed. Please install Lima or use a platform-specific locki wheel.")
    sys.exit(1)


def ensure_qemu_img() -> None:
    if shutil.which("qemu-img"):
        return
    logger.error("qemu-img is not installed on the host. Lima requires it to create and start the VM.")
    if sys.platform.startswith("linux"):
        logger.error("On Ubuntu/Debian, install it with: sudo apt install qemu-utils")
    elif sys.platform == "darwin":
        logger.error("On macOS, install it with: brew install qemu")
    else:
        logger.error("Please install qemu-img for your platform.")
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
            return
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
    cwd = pathlib.Path.cwd().resolve()
    if not cwd.is_relative_to(WORKTREES_HOME.resolve()):
        return None
    return WORKTREES_HOME / cwd.relative_to(WORKTREES_HOME).parts[0]


async def find_worktree_for_branch(branch: str) -> pathlib.Path | None:
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


def ensure_locki_dirs() -> None:
    LOCKI_HOME.mkdir(exist_ok=True)
    LIMA_HOME.mkdir(exist_ok=True, parents=True)
    WORKTREES_HOME.mkdir(parents=True, exist_ok=True)
    WORKTREES_META.mkdir(parents=True, exist_ok=True)
    # Lima shared mounts expect their source directories to exist before the VM boots.
    for provider in PROVIDERS.values():
        provider.host_state_dir.mkdir(parents=True, exist_ok=True)


async def ensure_vm_started() -> None:
    ensure_locki_dirs()
    ensure_qemu_img()
    async with _vm_lock():
        await run_command(
            [
                limactl(),
                "--tty=false",
                "create",
                str(data_file("locki.yaml")),
                "--mount-writable",
                "--name=locki",
            ],
            "Preparing VM",
            env={"LIMA_HOME": str(LIMA_HOME)},
            cwd="/",
            check=False,
        )
        await run_command(
            [limactl(), "--tty=false", "start", "locki"],
            "Starting VM",
            env={"LIMA_HOME": str(LIMA_HOME)},
            cwd="/",
        )


async def resolve_worktree(branch: str | None) -> pathlib.Path:
    if branch:
        wt_path = await find_worktree_for_branch(branch)
        if not wt_path:
            await run_command(["git", "-C", str(git_root()), "worktree", "prune"], "Pruning stale git worktrees")

            repo_name = git_root().name.replace("/", "-").replace(".", "-").lower()
            safe_branch = branch.replace("/", "-").replace(".", "-").lower()
            token = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            wt_id = f"{repo_name}--{safe_branch}--{token}"
            wt_path = WORKTREES_HOME / wt_id
            wt_path.mkdir(parents=True, exist_ok=True)

            result = await run_command(
                ["git", "-C", str(git_root()), "rev-parse", "--verify", f"refs/heads/{branch}"],
                f"Checking if branch '{branch}' exists",
                check=False,
            )
            if result.returncode != 0:
                await run_command(["git", "-C", str(git_root()), "branch", branch], f"Creating branch '{branch}'")

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

            hooks_dir = meta_dir / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            hook_script = data_file("locki-hook.sh").read_bytes()
            for name in GIT_HOOKS:
                hook_path = hooks_dir / name
                hook_path.write_bytes(hook_script)
                hook_path.chmod(0o755)

            await run_command(
                ["git", "-C", str(wt_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
                "Configuring per-worktree hooks",
            )
        return wt_path

    wt_path = current_worktree()
    if wt_path is None:
        logger.error("No branch specified and not inside a locki worktree.")
        sys.exit(1)
    return wt_path


def worktree_id(wt_path: pathlib.Path) -> str:
    return wt_path.relative_to(WORKTREES_HOME).parts[0]


async def ensure_disk_device(wt_id: str, device_name: str, source: str, path: str, message: str) -> None:
    current_source = await run_in_vm(
        ["incus", "config", "device", "get", wt_id, device_name, "source"],
        f"Checking {device_name} mount",
        check=False,
    )
    if current_source.returncode == 0 and current_source.stdout.decode().strip() == source:
        mounted = await run_in_vm(
            ["incus", "exec", wt_id, "--", "mountpoint", "-q", path],
            f"Verifying {device_name} mount",
            check=False,
        )
        if mounted.returncode == 0:
            return

    if current_source.returncode == 0:
        await run_in_vm(
            ["incus", "config", "device", "remove", wt_id, device_name],
            f"Updating {device_name} mount",
            check=False,
        )

    await run_in_vm(
        ["incus", "config", "device", "add", wt_id, device_name, "disk", f"source={source}", f"path={path}"],
        message,
    )


async def ensure_container(wt_path: pathlib.Path) -> str:
    wt_id = worktree_id(wt_path)
    config = load_config(git_root())

    result = await run_in_vm(["incus", "list", "--format=csv", "--columns=n", wt_id], "Checking container", check=False)
    exists = wt_id in result.stdout.decode().splitlines()

    if not exists:
        incus_image = config.get_incus_image()
        local_path = git_root() / incus_image
        if local_path.is_file():
            local_file = local_path.resolve()
            token = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            image_ref = f"locki-img-{token}"
            await run_command(
                [limactl(), "copy", str(local_file), f"locki:/tmp/{image_ref}"],
                "Copying image into VM",
                env={"LIMA_HOME": str(LIMA_HOME)},
                cwd="/",
            )
            await run_in_vm(
                ["bash", "-c", f"incus image import /tmp/{image_ref} --alias={image_ref} && rm -f /tmp/{image_ref}"],
                "Importing container image",
            )
        else:
            image_ref = incus_image

        await run_in_vm(["incus", "init", image_ref, wt_id], "Creating container")

        if local_path.is_file():
            await run_in_vm(["incus", "image", "delete", image_ref], "Cleaning up imported image", check=False)

    await run_in_vm(["incus", "start", wt_id], "Starting container", check=False)
    wt_parent = str(pathlib.PurePosixPath(str(wt_path)).parent)
    await run_in_vm(
        ["incus", "exec", wt_id, "--", "mkdir", "-p", wt_parent],
        "Preparing worktree mount path",
    )
    await ensure_disk_device(wt_id, "worktree", str(wt_path), str(wt_path), "Mounting worktree into container")
    return wt_id


def build_generic_container_files() -> dict[str, str]:
    stub = data_text("stub.sh")
    return {"/opt/locki/bin/git": stub, "/opt/locki/bin/gh": stub}


async def write_container_files(wt_id: str, files: dict[str, str]) -> None:
    for path, content in files.items():
        path_obj = pathlib.PurePosixPath(path)
        await run_in_vm(
            [
                "incus",
                "exec",
                wt_id,
                "--",
                "bash",
                "-c",
                f"mkdir -p {shlex.quote(str(path_obj.parent))} && cat >{shlex.quote(path)}",
            ],
            f"Writing {path_obj.name}",
            input=content.encode(),
        )


async def resolve_host_ip() -> str:
    return (
        (
            await run_in_vm(
                ["bash", "-c", "getent hosts host.lima.internal | awk '{print $1}' | head -1"],
                "Resolving host IP",
                check=False,
            )
        )
        .stdout.decode()
        .strip()
    )


def build_generic_container_setup(host_ip: str) -> str:
    return textwrap.dedent(
        f"""\
        for bin in /opt/locki/bin/*; do chmod +x "$bin"; done
        command -v mise >/dev/null || curl -fsSL https://mise.run | sh || true
        mkdir -p /etc/dnf || true
        grep -qxF 'cachedir=/var/cache/locki/dnf' /etc/dnf/dnf.conf 2>/dev/null || echo 'cachedir=/var/cache/locki/dnf' >> /etc/dnf/dnf.conf || true
        grep -qxF 'keepcache=1' /etc/dnf/dnf.conf 2>/dev/null || echo 'keepcache=1' >> /etc/dnf/dnf.conf || true
        mkdir -p /etc/apt/apt.conf.d || true
        cat >/etc/apt/apt.conf.d/99local-cache <<'EOF'
        Dir::Cache "/var/cache/locki/apt/cache";
        Dir::State "/var/cache/locki/apt/state";
        EOF
        grep -qxF '{host_ip} host.lima.internal' /etc/hosts || echo '{host_ip} host.lima.internal' >> /etc/hosts
        """
    )


async def run_container_setup(wt_id: str, script: str, message: str) -> None:
    await run_in_vm(
        ["incus", "exec", wt_id, "--", "bash", "-euxo", "pipefail", "-c", script],
        message,
    )


async def prepare_worktree_environment(branch: str | None) -> tuple[pathlib.Path, str]:
    git_root()
    await ensure_vm_started()
    wt_path = await resolve_worktree(branch)
    wt_id = await ensure_container(wt_path)
    await write_container_files(wt_id, build_generic_container_files())
    await run_container_setup(
        wt_id, build_generic_container_setup(await resolve_host_ip()), "Configuring container environment"
    )
    return wt_path, wt_id


async def prepare_provider_environment(wt_id: str, wt_path: pathlib.Path, provider: ProviderSpec) -> None:
    provider.host_state_dir.mkdir(parents=True, exist_ok=True)
    if provider.host_state_prep:
        provider.host_state_prep()

    guest_dir = str(provider.guest_state_dir)
    await ensure_disk_device(
        wt_id, provider.name, guest_dir, guest_dir, f"Mounting {provider.name} state into container"
    )
    await write_container_files(wt_id, provider.container_file_builder(wt_path))

    if provider.container_setup_builder:
        script = provider.container_setup_builder()
        if script:
            await run_container_setup(wt_id, script, f"Configuring {provider.name} environment")


def exec_in_container(wt_id: str, wt_path: pathlib.Path, inner_command: list[str]) -> typing.NoReturn:
    os.environ["LIMA_HOME"] = str(LIMA_HOME)
    os.environ["LIMA_SHELLENV_ALLOW"] = ",".join(FORWARDED_ENV)

    incus_exec = [
        "sudo",
        "incus",
        "exec",
        wt_id,
        "--cwd",
        str(wt_path),
        *(f"--env={env}=${env}" for env in FORWARDED_ENV),
        "--",
        *inner_command,
    ]
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
            "-lc",
            shlex.join(incus_exec),
        ],
    )


def exec_shell_session(
    wt_id: str, wt_path: pathlib.Path, command: str | None, extra_args: list[str]
) -> typing.NoReturn:
    if command is not None:
        inner_command = [
            "bash",
            "-lc",
            'eval "$(mise activate bash)" && exec bash -lc "$1" bash "${@:2}"',
            "--",
            command,
            *extra_args,
        ]
    elif extra_args:
        inner_command = ["bash", "-lc", 'eval "$(mise activate bash)" && exec bash "$@"', "--", *extra_args]
    else:
        inner_command = ["bash", "-lc", 'eval "$(mise activate bash)" && exec bash -i']
    exec_in_container(wt_id, wt_path, inner_command)


def exec_provider_session(wt_id: str, wt_path: pathlib.Path, provider_argv: list[str]) -> typing.NoReturn:
    exec_in_container(
        wt_id, wt_path, ["bash", "-lc", 'eval "$(mise activate bash)" && exec "$@"', "--", *provider_argv]
    )


async def run_provider_command(ctx: typer.Context, branch: str | None, provider_name: str) -> None:
    provider = PROVIDERS[provider_name]
    wt_path, wt_id = await prepare_worktree_environment(branch)
    await prepare_provider_environment(wt_id, wt_path, provider)
    exec_provider_session(wt_id, wt_path, provider.launch_argv_builder(wt_path, list(ctx.args)))


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
    wt_path, wt_id = await prepare_worktree_environment(branch)
    exec_shell_session(wt_id, wt_path, command, list(ctx.args))


@app.command("claude", context_settings={"allow_extra_args": True})
async def claude_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to work on (optional if inside a worktree)")
    ] = None,
):
    """Run Claude in the sandbox."""
    await run_provider_command(ctx, branch, "claude")


@app.command("codex", context_settings={"allow_extra_args": True})
async def codex_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to work on (optional if inside a worktree)")
    ] = None,
):
    """Run Codex in the sandbox."""
    await run_provider_command(ctx, branch, "codex")


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
            branch,
            wt_path,
        )
        sys.exit(1)

    if delete_branch and not branch:
        result = await run_command(
            ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            "Resolving branch name",
            check=False,
        )
        branch = result.stdout.decode().strip() if result.returncode == 0 else None

    wt_id = worktree_id(wt_path)

    await run_in_vm(["incus", "delete", "--force", wt_id], "Deleting container", check=False)

    shutil.rmtree(wt_path, ignore_errors=True)
    shutil.rmtree(WORKTREES_META / wt_id, ignore_errors=True)
    await run_command(["git", "-C", str(git_root()), "worktree", "prune"], "Removing worktree", check=False)

    if delete_branch and branch:
        await run_command(
            ["git", "-C", str(git_root()), "branch", "-D", branch], f"Deleting branch {branch}", check=False
        )


@app.command("list", help="List branches with locki-managed worktrees.")
async def list_cmd():
    result = await run_command(["git", "-C", str(git_root()), "worktree", "list", "--porcelain"], "Listing worktrees")

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
