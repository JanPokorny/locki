import base64
import contextlib
import getpass
import importlib.resources
import json
import logging
import os
import pathlib
import secrets
import shlex
import string
import subprocess
import sys
import tempfile
import time

import click

from locki.config import load_config
from locki.paths import DATA, HOME, LIMA, RUNTIME, WORKTREES, WORKTREES_META
from locki.utils import (
    cwd_git_repo,
    file_lock,
    limactl,
    resolve_sandbox,
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
    "MISE_GLOBAL_CONFIG_FILE": "/opt/locki/mise.toml",
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
@click.option("-i", "--interactive", "interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("-a", "--all", "all_repos", is_flag=True, default=False, help="Show sandboxes from all repos.")
@click.option("-c", "--create", is_flag=True, default=False, help="Create a new sandbox.")
@click.option("-f", "--id-file", default=None, type=click.Path(), help="Write the generated sandbox ID to this file.")
@click.pass_context
def exec_cmd(ctx, match, interactive, all_repos, create, id_file):
    """Run a command in the per-branch sandbox container.

    \b
    Examples:
      locki x bash                    # current sandbox, or picker / create
      locki x claude                  # run Claude Code
      locki x -m feat bash            # match sandbox by substring
      locki x -i bash                 # force sandbox picker even inside a worktree
      locki x -a bash                 # picker across all repos
      locki x -c bash                 # create new sandbox
      locki x bash -c "echo hello"    # run a one-liner
    """
    if create and (match or interactive or all_repos):
        click.echo(
            f"{click.style('ᛞ', fg='red', bold=True)} --create conflicts with --match/--interactive/--all.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"{click.style('ᚠ', fg='magenta', bold=True)} Entering a Locki sandbox.", err=True)

    if create:
        sandbox = None
    else:
        sandbox = resolve_sandbox(
            match=match,
            interactive=interactive,
            all_repos=all_repos,
            allow_create=True,
        )

    if sandbox is None:
        # Creating a new sandbox — cwd must be inside a git repo.
        repo_path = cwd_git_repo()
        if repo_path is None:
            click.echo(
                f"{click.style('ᛞ', fg='red', bold=True)} Cannot create a sandbox outside a git repo.",
                err=True,
            )
            sys.exit(1)
        wt_id = _gen_wt_id()
        branch = f"untitled#locki-{wt_id}"
        if id_file:
            pathlib.Path(id_file).write_text(wt_id)
        wt_path = WORKTREES / wt_id
    else:
        wt_id = sandbox.wt_id
        branch = sandbox.branch
        wt_path = sandbox.wt_path
        repo_path = sandbox.repo

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

    if not wt_path.exists():
        run_command(
            ["git", "-C", str(repo_path), "worktree", "prune"],
            "Pruning stale git worktrees",
        )

        wt_path.mkdir(parents=True, exist_ok=True)

        run_command(
            ["git", "-C", str(repo_path), "branch", branch],
            f"Creating branch {click.style(branch, fg='green')}",
        )
        run_command(
            ["git", "-C", str(repo_path), "worktree", "add", str(wt_path), branch],
            f"Creating worktree for {click.style(branch, fg='green')}",
        )

        locki_dir = wt_path / ".locki"
        locki_dir.mkdir(parents=True, exist_ok=True)
        (locki_dir / ".gitignore").write_text("*\n")

        meta_dir = WORKTREES_META / wt_id
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / ".git").write_text((wt_path / ".git").read_text())
        (meta_dir / "branch").write_text(branch)
        (meta_dir / "repo").write_text(str(repo_path))

        run_command(
            ["git", "-C", str(repo_path), "config", "extensions.worktreeConfig", "true"],
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

    config = load_config(repo_path)

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

        local_path = repo_path / incus_image
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

    # Idempotently start the Locki host daemon (SSH forced-command proxy + periodic cleanup).
    RUNTIME.mkdir(parents=True, exist_ok=True)
    client_ssh_dir = DATA / "home" / ".ssh"
    client_ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pid_file, port_file = RUNTIME / "daemon.pid", RUNTIME / "daemon.port"
    ssh_port = 0
    with file_lock("daemon", "Waiting for daemon start"):
        alive = False
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError, PermissionError, FileNotFoundError):
                os.kill(int(pid_file.read_text().strip()), 0)
                alive = True
        if not alive:
            pid_file.unlink(missing_ok=True)
            port_file.unlink(missing_ok=True)
            subprocess.Popen(
                [sys.executable, "-m", "locki", "internal", "daemon"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for _ in range(100):  # up to 10s for the daemon to write its port
            with contextlib.suppress(OSError, ValueError):
                if ssh_port := int(port_file.read_text().strip()):
                    break
            time.sleep(0.1)
    if not ssh_port:
        logger.warning("Locki daemon did not report a port in time. Self-service proxy is disabled in this sandbox.")
    (client_ssh_dir / "locki-ssh-config").write_text(
        (importlib.resources.files("locki") / "data" / "locki-ssh-config").read_text()
        + f"    Port {ssh_port}\n    User {getpass.getuser()}\n"
    )

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
    click.echo(f"{click.style('ᛃ', fg='cyan', bold=True)} Return to this sandbox:", err=True)
    click.echo(
        f"{click.style('ᛃ', fg='cyan', bold=True)}      via AI: {click.style(f'locki ai -m {wt_id}', fg='green')}"
        f" (or just {click.style('locki ai', fg='green')} and find it in the list)",
        err=True,
    )
    click.echo(
        f"{click.style('ᛃ', fg='cyan', bold=True)}   via shell: {click.style(f'locki x -m {wt_id}', fg='green')}",
        err=True,
    )
    try:
        wt_display = str(pathlib.Path("~") / (WORKTREES / wt_id).relative_to(HOME))
    except ValueError:
        wt_display = str(WORKTREES / wt_id)
    click.echo(f"{click.style('ᛃ', fg='cyan', bold=True)}     on disk: {click.style(wt_display, fg='green')}", err=True)
    raise SystemExit(result.returncode)
