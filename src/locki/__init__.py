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
import string
import subprocess
import sys
import textwrap
import typing

import anyio.to_thread
import typer
from halo import Halo

from locki.async_typer import AsyncTyper
from locki.config import load_config
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


SSH_PROXY_PORT = 7890
SSH_DIR = LOCKI_HOME / "ssh"


def _ensure_ssh_proxy():
    """Start a dedicated sshd that forwards allowed commands to the host."""
    SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    client_ssh_dir = LOCKI_HOME / "home" / ".ssh"
    client_ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Generate keys once
    host_key = SSH_DIR / "host_key"
    client_key = client_ssh_dir / "id_locki"
    if not host_key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(host_key), "-N", ""], check=True,
                        capture_output=True)
    if not client_key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(client_key), "-N", ""], check=True,
                        capture_output=True)

    # Install static SSH client config (shared into containers via ~/.locki/home)
    ssh_config_src = importlib.resources.files("locki") / "data" / "locki-ssh-config"
    ssh_config_dst = client_ssh_dir / "locki-ssh-config"
    if not ssh_config_dst.exists():
        ssh_config_dst.write_text(ssh_config_src.read_text())

    # Write authorized_keys with forced command (references generated pubkey)
    auth_keys = SSH_DIR / "authorized_keys"
    pub_key = client_key.with_suffix(".pub").read_text().strip()
    locki_bin = shutil.which("locki") or f"{sys.executable} -m locki"
    forced_cmd = f"{locki_bin} safe-cmd"
    auth_keys.write_text(
        f'command="{forced_cmd}",no-port-forwarding,no-X11-forwarding,no-agent-forwarding {pub_key}\n'
    )
    auth_keys.chmod(0o600)

    # Write sshd config (references generated host key + absolute paths)
    sshd_config = SSH_DIR / "sshd_config"
    pid_file = SSH_DIR / "sshd.pid"
    sshd_config.write_text(
        f"Port {SSH_PROXY_PORT}\n"
        f"ListenAddress 0.0.0.0\n"
        f"HostKey {host_key}\n"
        f"AuthorizedKeysFile {auth_keys}\n"
        f"PidFile {pid_file}\n"
        f"PasswordAuthentication no\n"
        f"PubkeyAuthentication yes\n"
        f"StrictModes no\n"
        f"UsePAM no\n"
        f"LogLevel ERROR\n"
    )

    # Start sshd if not already running
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            return
        except (ProcessLookupError, ValueError, PermissionError):
            pid_file.unlink(missing_ok=True)

    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    subprocess.Popen([sshd, "-f", str(sshd_config)], start_new_session=True)
    logger.info("Started SSH proxy (sshd) on port %d.", SSH_PROXY_PORT)


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


@app.command(
    "shell | sh | bash",
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
    setup_commands: list[tuple[list[str], str]] = getattr(ctx, "setup_commands", [])
    git_root()  # fail fast if not in a git repo

    sandbox_home = LOCKI_HOME / "home"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    claude_json_file = sandbox_home / ".claude.json"
    claude_json = json.loads(claude_json_file.read_text()) if claude_json_file.exists() else {}
    claude_json.setdefault("projects", {}).setdefault("/", {})["hasTrustDialogAccepted"] = True
    claude_json_file.write_text(json.dumps(claude_json, indent=2) + "\n")

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

        proxy_stub = (importlib.resources.files("locki") / "data" / "proxy-stub.sh").read_text()
        agents_md = (importlib.resources.files("locki") / "data" / "AGENTS.md").read_text()
        container_files = {
            "/etc/claude-code/CLAUDE.md": agents_md,
            "/etc/claude-code/managed-settings.json": json.dumps(
                {
                    "skipDangerousModePermissionPrompt": True,
                    "permissions": {"defaultMode": "bypassPermissions"},
                }
            ),
            "/etc/gemini-cli/GEMINI.md": agents_md,
            "/etc/gemini-cli/settings.json": json.dumps(
                {
                    "security": {"folderTrust": {"enabled": False}},
                    "tools": {"sandbox": False},
                }
            ),
            "/etc/codex/config.toml": textwrap.dedent(f"""\
                approval_policy = "never"
                sandbox_mode = "danger-full-access"
                cli_auth_credentials_store = "file"
                developer_instructions = "/etc/codex/AGENTS.md"
                projects.{json.dumps(str(WORKTREES_HOME))}.trust_level = "trusted"
            """),
            "/etc/codex/AGENTS.md": agents_md,
            "/opt/locki/bin/git": proxy_stub,
            "/opt/locki/bin/gh": proxy_stub,
            "/opt/locki/bin/bwrap": "#!/bin/sh\nexit 1\n",  # silence codex warning
        }
        for path, content in container_files.items():
            await run_in_vm(
                ["incus", "exec", wt_id, "--", "bash", "-c", f"mkdir -p $(dirname {path}) && cat >{path}"],
                f"Writing {pathlib.PurePosixPath(path).name}",
                input=content.encode(),
            )

        host_ip = (
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
                    hostnamectl set-hostname locki 2>/dev/null || echo locki > /etc/hostname
                    for bin in /opt/locki/bin/*; do chmod +x "$bin"; done
                    command -v mise || curl -fsSL https://mise.run | sh || true
                    mkdir -p /etc/dnf && echo -e "cachedir=/var/cache/locki/dnf\\nkeepcache=1" >> /etc/dnf/dnf.conf || true
                    mkdir -p /etc/apt/apt.conf.d && printf 'Dir::Cache "/var/cache/locki/apt/cache";\\nDir::State "/var/cache/locki/apt/state";\\n' > /etc/apt/apt.conf.d/99local-cache || true
                    echo '{host_ip} host.lima.internal' >> /etc/hosts
                """),
            ],
            "Configuring container environment",
        )

    for cmd, msg in setup_commands or []:
        await run_in_vm(["incus", "exec", wt_id, "--", *cmd], msg)

    _ensure_ssh_proxy()

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
    ctx.setup_commands = [
        (["mise", "install", "nodejs@24"], "Installing Node.js"),
        (
            ["mise", "exec", "nodejs@24", "--", "mise", "install", "npm:@anthropic-ai/claude-code@latest"],
            "Installing Claude Code CLI",
        ),
    ]
    await shell_cmd(
        ctx=ctx,
        branch=branch,
        command='exec mise exec nodejs@24 npm:@anthropic-ai/claude-code@latest -- claude "$@"',
    )


@app.command(
    "gemini",
    context_settings={"allow_extra_args": True},
)
async def gemini_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to work on (optional if inside a worktree)")
    ] = None,
):
    """Run Gemini in the sandbox."""
    ctx.setup_commands = [
        (["mise", "install", "nodejs@24"], "Installing Node.js"),
        (
            ["mise", "exec", "nodejs@24", "--", "mise", "install", "npm:@google/gemini-cli@latest"],
            "Installing Gemini CLI",
        ),
    ]
    await shell_cmd(
        ctx=ctx, branch=branch, command='exec mise exec nodejs@24 npm:@google/gemini-cli@latest -- gemini --yolo "$@"'
    )


@app.command(
    "codex",
    context_settings={"allow_extra_args": True},
)
async def codex_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to work on (optional if inside a worktree)")
    ] = None,
):
    """Run Codex in the sandbox."""
    ctx.setup_commands = [
        (["mise", "install", "nodejs@24"], "Installing Node.js"),
        (["mise", "exec", "nodejs@24", "--", "mise", "install", "npm:@openai/codex@latest"], "Installing Codex CLI"),
    ]
    await shell_cmd(
        ctx=ctx, branch=branch, command='exec mise exec nodejs@24 npm:@openai/codex@latest -- codex --yolo "$@"'
    )


@app.command("remove | rm | delete", help="Remove a branch's worktree and container.")
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


@app.command("list | ls", help="List branches with Locki-managed worktrees.")
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


vm_app = AsyncTyper(name="vm", help="Manage the Locki VM.", no_args_is_help=True)
app.add_typer(vm_app)


@vm_app.command("stop", help="Stop the Locki VM.")
async def vm_stop_cmd():
    await run_command(
        [limactl(), "stop", "locki"],
        "Stopping VM",
        env={"LIMA_HOME": str(LIMA_HOME)},
        cwd="/",
    )


@vm_app.command("delete | remove | rm", help="Delete the Locki VM entirely.")
async def vm_delete_cmd():
    await run_command(
        [limactl(), "delete", "-f", "locki"],
        "Deleting VM",
        env={"LIMA_HOME": str(LIMA_HOME)},
        cwd="/",
    )


@app.command("safe-cmd", hidden=True)
def safe_cmd():
    """SSH forced command: validate and execute an allowed git/gh command."""
    from locki.cmd_proxy import _validate_command, _validate_worktree

    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not cmd:
        print("No command specified.", file=sys.stderr)
        raise SystemExit(1)

    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        print(f"Failed to parse command: {e}", file=sys.stderr)
        raise SystemExit(1)

    if len(parts) < 2:
        print("Usage: <cwd> <exe> [args...]", file=sys.stderr)
        raise SystemExit(1)

    cwd_str, *argv = parts

    try:
        cwd = _validate_worktree(cwd_str)
        exe, args = _validate_command(argv)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)

    os.chdir(str(cwd))
    os.execvp(exe, [exe, *args])
