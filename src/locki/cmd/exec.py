import base64
import contextlib
import getpass
import importlib.resources
import json
import logging
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time

import click

from locki.config import load_config
from locki.paths import DATA, LIMA, PID_FILE, PORT_FILE, RUNTIME, WORKTREES
from locki.runes import EXIT, INFO, SPINNER
from locki.utils import (
    fail,
    file_lock,
    gen_id,
    limactl,
    pretty_path,
    resolve_sandbox,
    run_command,
    run_in_vm,
    setup_worktree_hooks,
    vm_status,
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

logger = logging.getLogger(__name__)


@click.command(
    "exec | x",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("-m", "--match", "match", default=None, help="Substring match on existing sandbox branch.")
@click.option("-i", "--interactive", "interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("-c", "--create", is_flag=True, default=False, help="Create a new sandbox.")
@click.option("-f", "--id-file", default=None, type=click.Path(), help="Write the generated sandbox ID to this file.")
@click.pass_context
def exec_cmd(ctx, match, interactive, create, id_file):
    """Run a command in the per-branch sandbox container.

    \b
    Examples:
      locki x bash                    # current sandbox, or picker / create
      locki x claude                  # run Claude Code
      locki x -m feat bash            # match sandbox by substring
      locki x -i bash                 # force sandbox picker even inside a worktree
      locki x -c bash                 # create new sandbox
      locki x bash -c "echo hello"    # run a one-liner
    """
    if create and (match or interactive):
        fail("--create conflicts with --match/--interactive.")

    click.echo(f"{SPINNER} Entering a Locki sandbox.", err=True)

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )
    if id_file and not sandbox.wt_path.exists():
        pathlib.Path(id_file).write_text(sandbox.wt_id)

    LIMA.mkdir(exist_ok=True, parents=True)
    WORKTREES.mkdir(parents=True, exist_ok=True)

    sandbox_home = DATA / "home"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    for path, content in [
        (sandbox_home / ".claude.json", '{ "projects": { "/": { "hasTrustDialogAccepted": true } } }'),
        (
            sandbox_home / ".claude" / "settings.json",
            '{ "skipDangerousModePermissionPrompt": true, "permissions": { "defaultMode": "bypassPermissions" } }',
        ),
        (
            sandbox_home / ".config" / "opencode" / "opencode.json",
            '{ "$schema": "https://opencode.ai/config.json", "permission": "allow", "instructions": "/etc/opencode/AGENTS.md" }',
        ),
    ]:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

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

    if vm_status() != "Running":
        fail(f"Lima VM failed to start. LIMA_HOME={LIMA}")

    if not sandbox.wt_path.exists():
        run_command(
            ["git", "-C", str(sandbox.repo), "worktree", "prune"],
            "Pruning stale git worktrees",
        )

        sandbox.wt_path.mkdir(parents=True, exist_ok=True)

        run_command(
            ["git", "-C", str(sandbox.repo), "branch", sandbox.branch],
            f"Creating branch {click.style(sandbox.branch, fg='green')}",
        )
        run_command(
            ["git", "-C", str(sandbox.repo), "worktree", "add", str(sandbox.wt_path), sandbox.branch],
            f"Creating worktree for {click.style(sandbox.branch, fg='green')}",
        )

        locki_dir = sandbox.wt_path / ".locki"
        locki_dir.mkdir(parents=True, exist_ok=True)
        (locki_dir / ".gitignore").write_text("*\n")

        sandbox.meta_path.mkdir(parents=True, exist_ok=True)
        (sandbox.meta_path / ".git").write_text((sandbox.wt_path / ".git").read_text())
        (sandbox.meta_path / "repo").write_text(str(sandbox.repo))

        setup_worktree_hooks(sandbox.repo, sandbox.meta_path, sandbox.wt_path)

    config = load_config(sandbox.repo)

    result = run_in_vm(
        ["incus", "list", "--format=csv", "--columns=n", sandbox.wt_id],
        "Checking container",
        check=False,
    )
    if sandbox.wt_id in result.stdout.decode():
        run_in_vm(
            ["incus", "start", sandbox.wt_id],
            "Starting container",
            check=False,
        )
    else:
        incus_image = config.get_incus_image()

        local_path = sandbox.repo / incus_image
        with file_lock("image", "Waiting for another image import"):
            if local_path.is_file():
                tmp_name = f"locki-img-{gen_id()}"
                run_command(
                    [limactl(), "copy", str(local_path.resolve()), f"locki:/tmp/{tmp_name}"],
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
                ["incus", "init", image_ref, sandbox.wt_id],
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
                sandbox.wt_id,
                "worktree",
                "disk",
                f"source={sandbox.wt_path}",
                f"path={sandbox.wt_path}",
            ],
            "Mounting worktree into container",
        )

        run_in_vm(
            ["incus", "start", sandbox.wt_id],
            "Starting container",
        )

        setup_script = (importlib.resources.files("locki") / "data" / "container-setup.sh").read_bytes()
        agents_md = (importlib.resources.files("locki") / "data" / "AGENTS.md").read_bytes()
        setup_script = setup_script.replace(b"__AGENTS_MD_B64__", base64.b64encode(agents_md))
        env_flags = [flag for k, v in CONTAINER_ENV.items() for flag in ("--env", f"{k}={v}")]
        run_in_vm(
            ["incus", "exec", sandbox.wt_id, *env_flags, "--env", f"LOCKI_WORKTREES_HOME={WORKTREES}", "--", "/bin/sh"],
            "Configuring container",
            input=setup_script,
        )

    # Idempotently start the Locki host daemon (SSH forced-command proxy + periodic cleanup).
    RUNTIME.mkdir(parents=True, exist_ok=True)
    client_ssh_dir = DATA / "home" / ".ssh"
    client_ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ssh_port = 0
    with file_lock("daemon", "Waiting for daemon start"):
        alive = False
        if PID_FILE.exists():
            with contextlib.suppress(ProcessLookupError, ValueError, PermissionError, FileNotFoundError):
                os.kill(int(PID_FILE.read_text().strip()), 0)
                alive = True
        if not alive:
            PID_FILE.unlink(missing_ok=True)
            PORT_FILE.unlink(missing_ok=True)
            subprocess.Popen(
                [sys.executable, "-m", "locki", "internal", "daemon"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for _ in range(100):  # up to 10s for the daemon to write its port
            with contextlib.suppress(OSError, ValueError):
                if ssh_port := int(PORT_FILE.read_text().strip()):
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
                    shlex.quote(sandbox.wt_id),
                    "--cwd",
                    shlex.quote(str(sandbox.wt_path)),
                    *(f"--env={k}={v}" for k, v in CONTAINER_ENV.items()),
                    *(f"--env={env}=${env}" for env in forwarded_env),
                    "--",
                    *((shlex.quote(a) for a in ctx.args) if ctx.args else ["bash"]),
                ]
            ),
        ],
    )

    click.echo()
    click.echo(f"{EXIT} Exited Locki sandbox.", err=True)
    click.echo(f"{INFO} Return to this sandbox:", err=True)
    click.echo(
        f"{INFO}      via AI: {click.style(f'locki ai -m {sandbox.wt_id}', fg='green')}"
        f" (or just {click.style('locki ai', fg='green')} and find it in the list)",
        err=True,
    )
    click.echo(
        f"{INFO}   via shell: {click.style(f'locki x -m {sandbox.wt_id}', fg='green')}",
        err=True,
    )
    click.echo(f"{INFO}     on disk: {click.style(pretty_path(sandbox.wt_path), fg='green')}", err=True)
    raise SystemExit(result.returncode)
