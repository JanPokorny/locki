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

import typer

import locki
from locki.config import load_config
from locki.utils import run_command

logger = logging.getLogger(__name__)


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
    locki.git_root()  # fail fast if not in a git repo

    sandbox_home = locki.LOCKI_HOME / "home"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    claude_json_file = sandbox_home / ".claude.json"
    claude_json = json.loads(claude_json_file.read_text()) if claude_json_file.exists() else {}
    claude_json.setdefault("projects", {}).setdefault("/", {})["hasTrustDialogAccepted"] = True
    claude_json_file.write_text(json.dumps(claude_json, indent=2) + "\n")

    locki.LOCKI_HOME.mkdir(exist_ok=True)
    locki.LIMA_HOME.mkdir(exist_ok=True, parents=True)
    locki.WORKTREES_HOME.mkdir(parents=True, exist_ok=True)
    async with locki._vm_lock():
        await run_command(
            [
                locki.limactl(),
                "--tty=false",
                "create",
                str(importlib.resources.files("locki").joinpath("data/locki.yaml")),
                "--mount-writable",
                "--name=locki",
            ],
            "Preparing VM",
            env={"LIMA_HOME": str(locki.LIMA_HOME)},
            cwd="/",
            check=False,
        )
        await run_command(
            [
                locki.limactl(),
                "--tty=false",
                "start",
                "locki",
            ],
            "Starting VM",
            env={"LIMA_HOME": str(locki.LIMA_HOME)},
            cwd="/",
            check=False,
        )

    if branch:
        wt_path = await locki.find_worktree_for_branch(branch)
        if not wt_path:
            await run_command(
                ["git", "-C", str(locki.git_root()), "worktree", "prune"],
                "Pruning stale git worktrees",
            )

            repo_name = locki.git_root().name.replace("/", "-").replace(".", "-").lower()
            safe_branch = branch.replace("/", "-").replace(".", "-").lower()
            wt_id = f"{repo_name}--{safe_branch}--{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
            wt_path = locki.WORKTREES_HOME / wt_id
            wt_path.mkdir(parents=True, exist_ok=True)

            result = await run_command(
                ["git", "-C", str(locki.git_root()), "rev-parse", "--verify", f"refs/heads/{branch}"],
                f"Checking if branch '{branch}' exists",
                check=False,
            )
            if result.returncode != 0:
                await run_command(
                    ["git", "-C", str(locki.git_root()), "branch", branch],
                    f"Creating branch '{branch}'",
                )

            await run_command(
                ["git", "-C", str(locki.git_root()), "worktree", "add", str(wt_path), branch],
                f"Creating worktree for '{branch}'",
            )

            meta_dir = locki.WORKTREES_META / wt_id
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / ".git").write_text((wt_path / ".git").read_text())

            await run_command(
                ["git", "-C", str(locki.git_root()), "config", "extensions.worktreeConfig", "true"],
                "Enabling per-worktree git config",
            )

            hooks_dir = locki.WORKTREES_META / wt_id / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            hook_script = (importlib.resources.files("locki") / "data" / "locki-hook.sh").read_bytes()
            for name in locki.GIT_HOOKS:
                hook_path = hooks_dir / name
                hook_path.write_bytes(hook_script)
                hook_path.chmod(0o755)

            await run_command(
                ["git", "-C", str(wt_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
                "Configuring per-worktree hooks",
            )
    else:
        wt_path = locki.current_worktree()
        if wt_path is None:
            print("No branch specified and not inside a locki worktree.", file=sys.stderr)
            sys.exit(1)
    wt_id = wt_path.relative_to(locki.WORKTREES_HOME).parts[0]

    config = load_config(locki.git_root())

    result = await locki.run_in_vm(
        ["incus", "list", "--format=csv", "--columns=n", wt_id],
        "Checking container",
        check=False,
    )
    if wt_id in result.stdout.decode():
        await locki.run_in_vm(
            ["incus", "start", wt_id],
            "Starting container",
            check=False,
        )
    else:
        incus_image = config.get_incus_image()

        local_path = locki.git_root() / incus_image
        if local_path.is_file():
            local_file = local_path.resolve()
            tmp_name = f"locki-img-{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
            await run_command(
                [locki.limactl(), "copy", str(local_file), f"locki:/tmp/{tmp_name}"],
                "Copying image into VM",
                env={"LIMA_HOME": str(locki.LIMA_HOME)},
                cwd="/",
            )
            await locki.run_in_vm(
                ["bash", "-c", f"incus image import /tmp/{tmp_name} --alias={tmp_name} && rm -f /tmp/{tmp_name}"],
                "Importing container image",
            )
            image_ref = tmp_name
        else:
            image_ref = incus_image

        await locki.run_in_vm(
            ["incus", "init", image_ref, wt_id],
            "Creating container",
        )

        if local_path.is_file():
            await locki.run_in_vm(
                ["incus", "image", "delete", image_ref],
                "Cleaning up imported image",
                check=False,
            )

        await locki.run_in_vm(
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

        await locki.run_in_vm(
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
                projects.{json.dumps(str(locki.WORKTREES_HOME))}.trust_level = "trusted"
            """),
            "/etc/codex/AGENTS.md": agents_md,
            "/opt/locki/bin/git": proxy_stub,
            "/opt/locki/bin/gh": proxy_stub,
            "/opt/locki/bin/bwrap": "#!/bin/sh\nexit 1\n",  # silence codex warning
        }
        for path, content in container_files.items():
            await locki.run_in_vm(
                ["incus", "exec", wt_id, "--", "bash", "-c", f"mkdir -p $(dirname {path}) && cat >{path}"],
                f"Writing {pathlib.PurePosixPath(path).name}",
                input=content.encode(),
            )

        host_ip = (
            (
                await locki.run_in_vm(
                    ["bash", "-c", "getent hosts host.lima.internal | awk '{print $1}' | head -1"],
                    "Resolving host IP",
                    check=False,
                )
            )
            .stdout.decode()
            .strip()
        )

        await locki.run_in_vm(
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
        await locki.run_in_vm(["incus", "exec", wt_id, "--", *cmd], msg)

    # Start SSH proxy (sshd) for git/gh command forwarding
    ssh_dir = locki.LOCKI_HOME / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    client_ssh_dir = locki.LOCKI_HOME / "home" / ".ssh"
    client_ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    host_key = ssh_dir / "host_key"
    client_key = client_ssh_dir / "id_locki"
    if not host_key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(host_key), "-N", ""], check=True, capture_output=True)
    if not client_key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(client_key), "-N", ""], check=True, capture_output=True)
    ssh_config_dst = client_ssh_dir / "locki-ssh-config"
    if not ssh_config_dst.exists():
        ssh_config_dst.write_text((importlib.resources.files("locki") / "data" / "locki-ssh-config").read_text())
    auth_keys = ssh_dir / "authorized_keys"
    locki_bin = shutil.which("locki") or f"{sys.executable} -m locki"
    auth_keys.write_text(
        f'command="{locki_bin} safe-cmd",no-port-forwarding,no-X11-forwarding,no-agent-forwarding '
        f"{client_key.with_suffix('.pub').read_text().strip()}\n"
    )
    auth_keys.chmod(0o600)
    pid_file = ssh_dir / "sshd.pid"
    (ssh_dir / "sshd_config").write_text(
        f"Port 7890\nListenAddress 0.0.0.0\nHostKey {host_key}\n"
        f"AuthorizedKeysFile {auth_keys}\nPidFile {pid_file}\n"
        f"PasswordAuthentication no\nPubkeyAuthentication yes\n"
        f"StrictModes no\nUsePAM no\nLogLevel ERROR\n"
    )
    sshd_running = False
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            sshd_running = True
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    sshd_path = shutil.which("sshd")
    if sshd_path is None:
        logger.warning("sshd was not found on the host. Safe git/gh proxy is disabled in this sandbox.")
    elif not sshd_running:
        subprocess.Popen([sshd_path, "-f", str(ssh_dir / "sshd_config")], start_new_session=True)

    forwarded_env = {"TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LANG", "SSH_TTY"}

    os.environ["LIMA_HOME"] = str(locki.LIMA_HOME)
    os.environ["LIMA_SHELLENV_ALLOW"] = ",".join(forwarded_env)

    os.execvp(
        locki.limactl(),
        [
            locki.limactl(),
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


async def claude_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[str | None, typer.Argument(help="Branch name")] = None,
):
    """Run Claude in the sandbox."""
    ctx.setup_commands = [
        (["mise", "install", "nodejs@24"], "Installing Node.js"),
        (["mise", "exec", "nodejs@24", "--", "mise", "install", "npm:@anthropic-ai/claude-code@latest"],
         "Installing Claude Code CLI"),
    ]
    await shell_cmd(ctx=ctx, branch=branch,
                    command='exec mise exec nodejs@24 npm:@anthropic-ai/claude-code@latest -- claude "$@"')


async def gemini_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[str | None, typer.Argument(help="Branch name")] = None,
):
    """Run Gemini in the sandbox."""
    ctx.setup_commands = [
        (["mise", "install", "nodejs@24"], "Installing Node.js"),
        (["mise", "exec", "nodejs@24", "--", "mise", "install", "npm:@google/gemini-cli@latest"],
         "Installing Gemini CLI"),
    ]
    await shell_cmd(ctx=ctx, branch=branch,
                    command='exec mise exec nodejs@24 npm:@google/gemini-cli@latest -- gemini --yolo "$@"')


async def codex_cmd(
    ctx: typer.Context,
    branch: typing.Annotated[str | None, typer.Argument(help="Branch name")] = None,
):
    """Run Codex in the sandbox."""
    ctx.setup_commands = [
        (["mise", "install", "nodejs@24"], "Installing Node.js"),
        (["mise", "exec", "nodejs@24", "--", "mise", "install", "npm:@openai/codex@latest"], "Installing Codex CLI"),
    ]
    await shell_cmd(ctx=ctx, branch=branch,
                    command='exec mise exec nodejs@24 npm:@openai/codex@latest -- codex --yolo "$@"')
