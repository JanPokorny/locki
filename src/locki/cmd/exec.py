import base64
import getpass
import importlib.resources
import json
import logging
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
import tempfile

import click

from locki.config import load_config
from locki.paths import DATA, LIMA, RUNTIME, STATE, WORKTREES, WORKTREES_META
from locki.utils import (
    current_worktree,
    file_lock,
    find_worktree_for_branch,
    git_root,
    limactl,
    list_locki_worktree_branches,
    match_sandbox_branch,
    run_command,
    run_in_vm,
)

CONTAINER_ENV = {
    "BUN_INSTALL_CACHE_DIR": "/var/cache/locki/bun",
    "BUNDLE_PATH": "/var/cache/locki/bundle",
    "CABAL_DIR": "/var/cache/locki/cabal",
    "CARGO_HOME": "/var/cache/locki/cargo",
    "COMPOSER_CACHE_DIR": "/var/cache/locki/composer",
    "CONAN_USER_HOME": "/var/cache/locki/conan",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "COURSIER_CACHE": "/var/cache/locki/coursier",
    "DENO_DIR": "/var/cache/locki/deno",
    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
    "GOCACHE": "/var/cache/locki/go/build",
    "GOMODCACHE": "/var/cache/locki/go/mod",
    "GRADLE_USER_HOME": "/var/cache/locki/gradle",
    "HEX_HOME": "/var/cache/locki/hex",
    "IS_SANDBOX": "1",
    "JULIA_DEPOT_PATH": "/var/cache/locki/julia",
    "LEIN_HOME": "/var/cache/locki/lein",
    "MAVEN_OPTS": "-Dmaven.repo.local=/var/cache/locki/maven",
    "MISE_CACHE_DIR": "/var/cache/locki/mise",
    "MISE_DATA": "/usr/share/mise",
    "MISE_INSTALL_PATH": "/usr/local/bin/mise",
    "MISE_NODE_VERIFY": "false",
    "MISE_TRUSTED_CONFIG_PATHS": "/",
    "MIX_HOME": "/var/cache/locki/mix",
    "NIMBLE_DIR": "/var/cache/locki/nimble",
    "npm_config_cache": "/var/cache/locki/npm",
    "NUGET_PACKAGES": "/var/cache/locki/nuget",
    "PATH": "/opt/locki/bin:/root/.local/bin:/usr/share/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/locki/bin/jit",
    "PIP_CACHE_DIR": "/var/cache/locki/pip",
    "PNPM_HOME": "/usr/share/pnpm",
    "PUB_CACHE": "/var/cache/locki/pub",
    "R_LIBS_USER": "/var/cache/locki/r",
    "REBAR_CACHE_DIR": "/var/cache/locki/rebar3",
    "STACK_ROOT": "/var/cache/locki/stack",
    "TF_PLUGIN_CACHE_DIR": "/var/cache/locki/terraform",
    "UV_CACHE_DIR": "/var/cache/locki/uv",
    "VCPKG_DEFAULT_BINARY_CACHE": "/var/cache/locki/vcpkg",
    "XDG_DATA_HOME": "/usr/share",
    "XDG_CACHE_HOME": "/var/cache/locki",
    "XDG_BIN_HOME": "/usr/local/bin",
    "YARN_CACHE_FOLDER": "/var/cache/locki/yarn",
    "ZIG_GLOBAL_CACHE_DIR": "/var/cache/locki/zig",
}

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

logger = logging.getLogger(__name__)


def _gen_wt_id() -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))


@click.command(
    "exec | x",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("-m", "--match", "match", default=None, help="Substring match on existing sandbox branch.")
@click.option("-s", "--select", is_flag=True, default=False, help="Show interactive sandbox selector.")
@click.option("-c", "--create", is_flag=True, default=False, help="Create a new sandbox.")
@click.option("-f", "--id-file", default=None, type=click.Path(), help="Write the generated sandbox ID to this file.")
@click.pass_context
def exec_cmd(ctx, match, select, create, id_file):
    """Run a command in the per-branch sandbox container.

    \b
    Examples:
      locki x bash                    # interactive shell (sandbox picker)
      locki x claude                  # run Claude Code
      locki x -m feat bash            # match sandbox by substring
      locki x -s bash                 # force sandbox selector
      locki x -c bash                 # create new sandbox
      locki x bash -c "echo hello"    # run a one-liner
    """
    if create and match:
        click.echo(
            f"{click.style('ᛞ', fg='red', bold=True)} --create and --match cannot be used together.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"{click.style('ᚠ', fg='magenta', bold=True)} Entering a Locki sandbox.", err=True)
    wt_id: str | None = None
    branch: str | None = None
    if create:
        wt_id = _gen_wt_id()
        branch = f"untitled#locki-{wt_id}"
        if id_file:
            pathlib.Path(id_file).write_text(wt_id)
    elif match:
        branch = match_sandbox_branch(match)
    elif select or not current_worktree():
        if not sys.stdin.isatty():
            click.echo(
                f"{click.style('ᛞ', fg='red', bold=True)} No branch specified. Use -m <query> in non-interactive mode.",
                file=sys.stderr,
            )
            sys.exit(1)

        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice

        wt_branches = list_locki_worktree_branches()

        choices = [Choice(value=None, name="(create new)")] + [Choice(value=b, name=b) for b in sorted(wt_branches)]

        selected = inquirer.fuzzy(
            message="Select a sandbox:",
            choices=choices,
        ).execute()

        if selected is None:
            wt_id = _gen_wt_id()
            branch = f"untitled#locki-{wt_id}"
        else:
            branch = selected

    git_root()  # fail fast if not in a git repo

    LIMA.mkdir(exist_ok=True, parents=True)
    WORKTREES.mkdir(parents=True, exist_ok=True)

    sandbox_home = DATA / "home"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    if not (claude_json_file := sandbox_home / ".claude.json").exists():
        claude_json_file.write_text('{ "projects": { "/": { "hasTrustDialogAccepted": true } } }')
    if not (claude_settings_file := sandbox_home / ".claude" / "settings.json").exists():
        claude_settings_file.parent.mkdir(parents=True, exist_ok=True)
        claude_settings_file.write_text(
            '{ "skipDangerousModePermissionPrompt": true, "permissions": { "defaultMode": "bypassPermissions" } }'
        )
    if not (opencode_config_file := sandbox_home / ".config" / "opencode" / "opencode.json").exists():
        opencode_config_file.parent.mkdir(parents=True, exist_ok=True)
        opencode_config_file.write_text(
            '{ "$schema": "https://opencode.ai/config.json", "permission": "allow", "instructions": "/etc/opencode/AGENTS.md" }'
        )

    with file_lock("vm", "Waiting for VM to start"):
        vm_setup = (importlib.resources.files("locki") / "data" / "vm-setup.sh").read_text()
        lima_config = json.dumps(
            {
                "minimumLimaVersion": "2.0.0",
                "base": ["template:fedora"],
                "containerd": {"system": False, "user": False},
                "mounts": [
                    {"location": str(WORKTREES), "writable": True},
                    {"location": str(sandbox_home), "mountPoint": "/root/.locki/home", "writable": True},
                ],
                "provision": [{"mode": "system", "script": vm_setup}],
            }
        )
        lima_fd, lima_yaml = tempfile.mkstemp(suffix=".yaml")
        try:
            os.write(lima_fd, lima_config.encode())
            os.close(lima_fd)
            run_command(
                [limactl(), "--tty=false", "create", lima_yaml, "--mount-writable", "--name=locki"],
                "Preparing VM",
                cwd="/",
                check=False,
            )
        finally:
            os.unlink(lima_yaml)
        run_command(
            [limactl(), "--tty=false", "start", "locki"],
            "Starting VM",
            cwd="/",
            check=False,
        )

    # Verify the VM is actually running before proceeding
    verify = run_command(
        [limactl(), "list", "--json"],
        "Verifying VM",
        cwd="/",
        check=False,
        quiet=True,
    )
    vm_running = False
    for line in verify.stdout.decode().splitlines():
        try:
            vm = json.loads(line)
            if vm.get("name") == "locki" and vm.get("status") == "Running":
                vm_running = True
        except json.JSONDecodeError:
            pass
    if not vm_running:
        logger.error("Lima VM failed to start. LIMA_HOME=%s", LIMA)
        sys.exit(1)

    wt_path = find_worktree_for_branch(branch) if branch else current_worktree()
    if not wt_path:  # branch was provided but does not exit
        if not branch:
            click.echo(
                f"{click.style('ᛞ', fg='red', bold=True)} No branch specified and not inside a Locki worktree.",
                file=sys.stderr,
            )
            sys.exit(1)

        run_command(
            ["git", "-C", str(git_root()), "worktree", "prune"],
            "Pruning stale git worktrees",
        )

        if not wt_id:
            wt_id = _gen_wt_id()
        wt_path = WORKTREES / wt_id
        wt_path.mkdir(parents=True, exist_ok=True)

        run_command(
            ["git", "-C", str(git_root()), "branch", branch],
            f"Creating branch {click.style(branch, fg='green')}",
        )
        run_command(
            ["git", "-C", str(git_root()), "worktree", "add", str(wt_path), branch],
            f"Creating worktree for {click.style(branch, fg='green')}",
        )

        locki_dir = wt_path / ".locki"
        locki_dir.mkdir(parents=True, exist_ok=True)
        (locki_dir / ".gitignore").write_text("*\n")

        meta_dir = WORKTREES_META / wt_id
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / ".git").write_text((wt_path / ".git").read_text())
        (meta_dir / "branch").write_text(branch)
        (meta_dir / "repo").write_text(str(git_root()))

        run_command(
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

        run_command(
            ["git", "-C", str(wt_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
            "Configuring per-worktree hooks",
        )

        run_command(
            ["git", "-C", str(wt_path), "config", "--worktree", "push.autoSetupRemote", "true"],
            "Configuring auto push for new branches",
        )

    wt_id = wt_path.relative_to(WORKTREES).parts[0]

    config = load_config(git_root())

    result = run_in_vm(
        ["incus", "list", "--format=csv", "--columns=n", wt_id],
        "Checking container",
        check=False,
    )
    if wt_id in result.stdout.decode():
        run_in_vm(
            ["incus", "start", wt_id],
            "Starting container",
            check=False,
        )
    else:
        incus_image = config.get_incus_image()

        local_path = git_root() / incus_image
        with file_lock("image", "Waiting for another image import"):
            if local_path.is_file():
                local_file = local_path.resolve()
                tmp_name = (
                    f"locki-img-{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
                )
                run_command(
                    [limactl(), "copy", str(local_file), f"locki:/tmp/{tmp_name}"],
                    "Copying image into VM",
                    cwd="/",
                )
                run_in_vm(
                    [
                        "bash",
                        "-c",
                        f"out=$(incus image import /tmp/{tmp_name} --alias={tmp_name} 2>&1) || echo \"$out\" | grep -q 'already exists'; rm -f /tmp/{tmp_name}",
                    ],
                    "Importing container image",
                )
                image_ref = tmp_name
            else:
                image_ref = incus_image

            run_in_vm(
                ["incus", "init", image_ref, wt_id],
                "Creating container",
            )

            if local_path.is_file():
                run_in_vm(
                    ["incus", "image", "delete", image_ref],
                    "Cleaning up imported image",
                    check=False,
                )

        run_in_vm(
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

        run_in_vm(
            ["incus", "start", wt_id],
            "Starting container",
        )

        setup_script = (importlib.resources.files("locki") / "data" / "container-setup.sh").read_bytes()
        agents_md = (importlib.resources.files("locki") / "data" / "AGENTS.md").read_bytes()
        setup_script = setup_script.replace(b"__AGENTS_MD_B64__", base64.b64encode(agents_md))
        env_flags = [flag for k, v in CONTAINER_ENV.items() for flag in ("--env", f"{k}={v}")]
        run_in_vm(
            ["incus", "exec", wt_id, *env_flags, "--env", f"LOCKI_WORKTREES_HOME={WORKTREES}", "--", "/bin/sh"],
            "Configuring container",
            input=setup_script,
        )

    # Start SSH proxy (sshd) for git/gh command forwarding
    ssh_dir = STATE / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    client_ssh_dir = DATA / "home" / ".ssh"
    client_ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    host_key = ssh_dir / "host_key"
    client_key = client_ssh_dir / "id_locki"
    if not host_key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(host_key), "-N", ""], check=True, capture_output=True)
    if not client_key.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(client_key), "-N", ""], check=True, capture_output=True
        )
    auth_keys = ssh_dir / "authorized_keys"
    locki_bin = shutil.which("locki") or f"{sys.executable} -m locki"
    # Forward HOME and any XDG base-dir vars so self-service resolves the same paths.
    # sshd strips env by default; only vars baked into the command= reach the child.
    cmd_env = f"HOME={shlex.quote(str(pathlib.Path.home()))}"
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        val = os.environ.get(var)
        if val:
            cmd_env += f" {var}={shlex.quote(val)}"
    auth_keys.write_text(
        f'command="{cmd_env} {locki_bin} self-service",no-port-forwarding,no-X11-forwarding,no-agent-forwarding '
        f"{client_key.with_suffix('.pub').read_text().strip()}\n"
    )
    auth_keys.chmod(0o600)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    pid_file = RUNTIME / "sshd.pid"
    sshd_running = False
    ssh_port = 0
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            sshd_running = True
            match = re.search(r"^Port (\d+)", (ssh_dir / "sshd_config").read_text(), re.MULTILINE)
            if match:
                ssh_port = int(match.group(1))
        except ProcessLookupError, ValueError, PermissionError, FileNotFoundError:
            pass
    sshd_path = shutil.which("sshd")
    if sshd_path is None:
        logger.warning("sshd was not found on the host. Self-service proxy is disabled in this sandbox.")
    elif not sshd_running:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            ssh_port = s.getsockname()[1]
        (ssh_dir / "sshd_config").write_text(
            f"Port {ssh_port}\nListenAddress 0.0.0.0\nHostKey {host_key}\n"
            f"AuthorizedKeysFile {auth_keys}\nPidFile {pid_file}\n"
            f"PasswordAuthentication no\nPubkeyAuthentication yes\n"
            f"StrictModes no\nUsePAM no\nLogLevel ERROR\n"
        )
        subprocess.Popen([sshd_path, "-f", str(ssh_dir / "sshd_config")], start_new_session=True)
    ssh_config_dst = client_ssh_dir / "locki-ssh-config"
    ssh_config_template = (importlib.resources.files("locki") / "data" / "locki-ssh-config").read_text()
    ssh_config_dst.write_text(ssh_config_template + f"    Port {ssh_port}\n    User {getpass.getuser()}\n")

    forwarded_env = {"TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LANG", "SSH_TTY"}

    os.environ["LIMA_SHELLENV_ALLOW"] = ",".join(forwarded_env)

    result = subprocess.run(
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
                    *(f"--env={k}={v}" for k, v in CONTAINER_ENV.items()),
                    *(f"--env={env}=${env}" for env in forwarded_env),
                    "--",
                    *((shlex.quote(a) for a in ctx.args) if ctx.args else ["bash"]),
                ]
            ),
        ],
    )

    click.echo()
    click.echo(f"{click.style('ᛟ', fg='magenta', bold=True)} Exited Locki sandbox.", err=True)
    click.echo(f"{click.style('ᛃ', fg='cyan', bold=True)} Return to this worktree:", err=True)
    click.echo(
        f"{click.style('ᛃ', fg='cyan', bold=True)}      via AI: {click.style(f'locki ai{f" -b {wt_id}" if branch else ""}', fg='green')}"
        + (f" (or just {click.style('locki ai', fg='green')} and find it in the list)" if branch else ""),
        err=True,
    )
    click.echo(
        f"{click.style('ᛃ', fg='cyan', bold=True)}   via shell: {click.style(f'locki x {f" -b {wt_id}" if branch else ""}', fg='green')}",
        err=True,
    )
    wt_full = WORKTREES / wt_id
    try:
        wt_display = "~/" + str(wt_full.relative_to(pathlib.Path.home()))
    except ValueError:
        wt_display = str(wt_full)
    click.echo(f"{click.style('ᛃ', fg='cyan', bold=True)}     on disk: {click.style(wt_display, fg='green')}", err=True)
    raise SystemExit(result.returncode)
