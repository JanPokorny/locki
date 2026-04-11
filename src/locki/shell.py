import getpass
import importlib.resources
import json
import logging
import os
import pathlib
import random
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import textwrap

import click

import locki
from locki.config import load_config
from locki.utils import run_command

logger = logging.getLogger(__name__)

_MALE_NAMES = [
    "aksel", "amund", "anders", "arne", "arvid", "asbjorn", "asgeir", "audun", "bard", "bersi",
    "birger", "bjarni", "bjorn", "bragi", "brand", "egil", "erik", "eyvind", "finn", "floki",
    "frode", "gardar", "gisli", "gorm", "grettir", "grimm", "gudmund", "gunnar", "guthorm",
    "hakon", "haldor", "halfdan", "hallvard", "halsten", "harald", "havard", "helgi", "hemming",
    "hermod", "hjalmar", "hoskuld", "hrolf", "ingjald", "ingvar", "ivar", "jansen", "jorgen",
    "karl", "ketil", "kirk", "knut", "kolbein", "leif", "ljot", "magnus", "naddod", "njal",
    "norman", "olaf", "orvar", "osgood", "ottar", "ragnar", "ragnvald", "rayner", "roderick",
    "roger", "rollo", "rurik", "rutland", "saxe", "sigmund", "sigurd", "sigvald", "skarde",
    "skorri", "skuli", "snorri", "starkad", "steinar", "sten", "styrbjorn", "sune", "svein",
    "sven", "thorfinn", "thormod", "thorsten", "thorvald", "toki", "torbjorn", "torfi",
    "torstein", "torvald", "tryggvi", "ulf", "vagn", "vemund", "vidar", "viggo",
]
_FEMALE_NAMES = [
    "alfhild", "alva", "anneli", "annika", "arnbjorg", "asdis", "aslaug", "asta", "astrid",
    "aud", "bergljot", "birgitta", "borghild", "brynhild", "dagny", "dahlia", "dalla", "disa",
    "edda", "embla", "erika", "erna", "freya", "frida", "geira", "gertrud", "gudrid", "gudrun",
    "gunhild", "gunnvor", "gyda", "hallgerd", "hallveig", "helga", "herdis", "hervor", "hilda",
    "hjordis", "hrefna", "idonea", "idun", "inga", "ingibjorg", "ingrid", "jofrid", "jorid",
    "jorunn", "kara", "katla", "lagertha", "liv", "nanna", "oda", "oddny", "ragnfrid",
    "ragnhild", "ran", "rayna", "revna", "runa", "saeunn", "sassa", "shelby", "sif", "sigfrid",
    "sigrid", "solveig", "steinunn", "sunniva", "svanhild", "thordis", "thorhild", "thorgerd",
    "thurid", "thyra", "tora", "torborg", "torunn", "unn", "vala", "valdis", "valkyrie",
    "vigdis", "vigga", "ylva", "yrsa",
]
_NEUTRAL_NAMES = [
    "agnar", "ari", "aslak", "bertil", "birk", "bo", "bodil", "brok", "bui", "crosby", "dag",
    "dagmar", "darby", "ebbe", "eilif", "einar", "esben", "eyolf", "folke", "geir", "gro",
    "hauk", "heid", "hreidar", "kai", "keld", "loki", "nanne", "njord", "orm", "randi", "roald",
    "rune", "saga", "sindri", "skald", "skai", "skjold", "sondre", "storm", "sverre", "thrain",
    "torkel", "tove", "tyr", "ull", "valdimar", "vigg", "whitby",
]


def _viking_name() -> str:
    """Generate a Viking-style name: <name>-<father>(sson|sdottir)-<number>."""
    is_male = random.choice([True, False])
    name = random.choice((_MALE_NAMES if is_male else _FEMALE_NAMES) + _NEUTRAL_NAMES)
    father = random.choice(_MALE_NAMES + _NEUTRAL_NAMES)
    suffix = "sson" if is_male else "sdottir"
    number = random.randint(1, 99)
    return f"{name}-{father}{suffix}-{number}"


@click.command("exec | x", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@click.option("-b", "--branch", default=None, help="Branch name to work on.")
@click.pass_context
def exec_cmd(ctx, branch):
    """Run a command in the per-branch sandbox container.

    \b
    Examples:
      locki x bash                    # interactive shell
      locki x claude                  # run Claude Code
      locki x -b my-feature bash      # specify branch
      locki x bash -c "echo hello"    # run a one-liner
    """
    if not branch:
        wt_path = locki.current_worktree()
        if wt_path is None:
            # In root repo, auto-generate a Viking branch name (avoid existing branches)
            existing = subprocess.run(
                ["git", "-C", str(locki.git_root()), "branch", "--list", "--all", "--format=%(refname:short)"],
                capture_output=True, text=True,
            ).stdout.splitlines()
            existing_set = {b.strip().removeprefix("origin/") for b in existing}
            for _ in range(100):
                branch = _viking_name()
                if branch not in existing_set:
                    break
            click.echo(f"Creating a new branch '{branch}'", err=True)

    locki.git_root()  # fail fast if not in a git repo

    locki.LOCKI_HOME.mkdir(exist_ok=True)
    locki.LIMA_HOME.mkdir(exist_ok=True, parents=True)
    locki.WORKTREES_HOME.mkdir(parents=True, exist_ok=True)

    sandbox_home = locki.LOCKI_HOME / "home"
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

    with locki._file_lock("vm", "Waiting for VM to start"):
        run_command(
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
        run_command(
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
        wt_path = locki.find_worktree_for_branch(branch)
        if not wt_path:
            run_command(
                ["git", "-C", str(locki.git_root()), "worktree", "prune"],
                "Pruning stale git worktrees",
            )

            repo_name = re.sub(r"[^a-z0-9-]", "-", locki.git_root().name.lower())
            safe_branch = re.sub(r"[^a-z0-9-]", "-", branch.lower())
            wt_id = f"{(f'{repo_name}--{safe_branch}'[:53].rstrip('-'))}--{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
            wt_path = locki.WORKTREES_HOME / wt_id
            wt_path.mkdir(parents=True, exist_ok=True)

            run_command(
                ["git", "-C", str(locki.git_root()), "fetch"],
                "Fetching from remote",
                check=False,
            )

            result = run_command(
                ["git", "-C", str(locki.git_root()), "worktree", "add", str(wt_path), branch],
                f"Creating worktree for '{branch}'",
                check=False,
            )
            if result.returncode != 0:
                run_command(
                    ["git", "-C", str(locki.git_root()), "branch", branch],
                    f"Creating branch '{branch}'",
                )
                run_command(
                    ["git", "-C", str(locki.git_root()), "worktree", "add", str(wt_path), branch],
                    f"Creating worktree for '{branch}'",
                )

            meta_dir = locki.WORKTREES_META / wt_id
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / ".git").write_text((wt_path / ".git").read_text())
            (meta_dir / "branch").write_text(branch)
            (meta_dir / "repo").write_text(str(locki.git_root()))

            run_command(
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

            run_command(
                ["git", "-C", str(wt_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
                "Configuring per-worktree hooks",
            )

            run_command(
                ["git", "-C", str(wt_path), "config", "--worktree", "push.autoSetupRemote", "true"],
                "Configuring auto push for new branches",
            )
    else:
        wt_path = locki.current_worktree()
        if wt_path is None:
            print("No branch specified and not inside a locki worktree.", file=sys.stderr)
            sys.exit(1)
    wt_id = wt_path.relative_to(locki.WORKTREES_HOME).parts[0]

    config = load_config(locki.git_root())

    result = locki.run_in_vm(
        ["incus", "list", "--format=csv", "--columns=n", wt_id],
        "Checking container",
        check=False,
    )
    if wt_id in result.stdout.decode():
        locki.run_in_vm(
            ["incus", "start", wt_id],
            "Starting container",
            check=False,
        )
    else:
        incus_image = config.get_incus_image()

        local_path = locki.git_root() / incus_image
        with locki._file_lock("image", "Waiting for another image import"):
            if local_path.is_file():
                local_file = local_path.resolve()
                tmp_name = (
                    f"locki-img-{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
                )
                run_command(
                    [locki.limactl(), "copy", str(local_file), f"locki:/tmp/{tmp_name}"],
                    "Copying image into VM",
                    env={"LIMA_HOME": str(locki.LIMA_HOME)},
                    cwd="/",
                )
                locki.run_in_vm(
                    ["bash", "-c", f"incus image import /tmp/{tmp_name} --alias={tmp_name} && rm -f /tmp/{tmp_name}"],
                    "Importing container image",
                )
                image_ref = tmp_name
            else:
                image_ref = incus_image

            locki.run_in_vm(
                ["incus", "init", image_ref, wt_id],
                "Creating container",
            )

            if local_path.is_file():
                locki.run_in_vm(
                    ["incus", "image", "delete", image_ref],
                    "Cleaning up imported image",
                    check=False,
                )

        locki.run_in_vm(
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

        locki.run_in_vm(
            ["incus", "start", wt_id],
            "Starting container",
        )

        proxy_stub = (importlib.resources.files("locki") / "data" / "proxy-stub.sh").read_text()
        agents_md = (importlib.resources.files("locki") / "data" / "AGENTS.md").read_text()
        container_files = {
            "/etc/claude-code/CLAUDE.md": agents_md,
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
            "/etc/opencode/AGENTS.md": agents_md,
            "/opt/locki/bin/git": proxy_stub,
            "/opt/locki/bin/gh": proxy_stub,
            "/opt/locki/bin/bwrap": "#!/bin/sh\nexit 1\n",  # silence codex warning
            "/opt/locki/bin/dnf": textwrap.dedent("""\
                #!/bin/bash
                set -euo pipefail
                real_dnf=$(PATH=$(echo "$PATH" | tr : '\\n' | grep -vx /opt/locki/bin | paste -sd:) command -v dnf)
                if [ -z "$real_dnf" ]; then echo "dnf: not found" >&2; exit 127; fi
                "$real_dnf" install -y \\
                  https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm \\
                  https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm \\
                  2>/dev/null || true
                rm -f /opt/locki/bin/dnf
                exec "$real_dnf" "$@"
            """),
            "/etc/bashrc.d/locki-mise.sh": 'eval "$(mise activate bash)"\n',
            "/opt/locki/bin/claude": textwrap.dedent("""\
                #!/bin/bash
                mise install nodejs@24 >&2
                mise exec nodejs@24 -- mise install npm:@anthropic-ai/claude-code@latest >&2
                exec mise exec nodejs@24 npm:@anthropic-ai/claude-code@latest -- claude --dangerously-skip-permissions "$@"
            """),
            "/opt/locki/bin/gemini": textwrap.dedent("""\
                #!/bin/bash
                mise install nodejs@24 >&2
                mise exec nodejs@24 -- mise install npm:@google/gemini-cli@latest >&2
                exec mise exec nodejs@24 npm:@google/gemini-cli@latest -- gemini --yolo "$@"
            """),
            "/opt/locki/bin/codex": textwrap.dedent("""\
                #!/bin/bash
                mise install nodejs@24 >&2
                mise exec nodejs@24 -- mise install npm:@openai/codex@latest >&2
                exec mise exec nodejs@24 npm:@openai/codex@latest -- codex --yolo "$@"
            """),
            "/opt/locki/bin/opencode": textwrap.dedent("""\
                #!/bin/bash
                mise use -g github:anomalyco/opencode >&2
                exec opencode "$@"
            """),
        }
        for path, content in container_files.items():
            locki.run_in_vm(
                ["incus", "exec", wt_id, "--", "bash", "-c", f"mkdir -p $(dirname {path}) && cat >{path}"],
                f"Writing {pathlib.PurePosixPath(path).name}",
                input=content.encode(),
            )

        host_ip = (
            locki.run_in_vm(
                ["bash", "-c", "getent hosts host.lima.internal | awk '{print $1}' | head -1"],
                "Resolving host IP",
                check=False,
            )
            .stdout.decode()
            .strip()
        )

        locki.run_in_vm(
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
                    if ! command -v mise &>/dev/null; then
                      if command -v dnf &>/dev/null; then dnf -y copr enable jdxcode/mise && dnf -y install mise
                      elif command -v apt &>/dev/null; then apt update -y && apt install -y mise
                      elif command -v pacman &>/dev/null; then pacman -Sy --noconfirm mise
                      elif command -v apk &>/dev/null; then apk add mise
                      elif command -v zypper &>/dev/null; then zypper --non-interactive install mise
                      else curl -fsSL https://mise.run | sh
                      fi
                    fi
                    mkdir -p /etc/dnf && echo -e "cachedir=/var/cache/locki/dnf\\nkeepcache=1" >> /etc/dnf/dnf.conf || true
                    mkdir -p /etc/apt/apt.conf.d && printf 'Dir::Cache "/var/cache/locki/apt/cache";\\nDir::State "/var/cache/locki/apt/state";\\n' > /etc/apt/apt.conf.d/99local-cache || true
                    echo '{host_ip} host.lima.internal' >> /etc/hosts
                """),
            ],
            "Configuring container environment",
        )

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
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(client_key), "-N", ""], check=True, capture_output=True
        )
    ssh_config_dst = client_ssh_dir / "locki-ssh-config"
    ssh_config_template = (importlib.resources.files("locki") / "data" / "locki-ssh-config").read_text()
    ssh_config_dst.write_text(ssh_config_template + f"    User {getpass.getuser()}\n")
    auth_keys = ssh_dir / "authorized_keys"
    locki_bin = shutil.which("locki") or f"{sys.executable} -m locki"
    auth_keys.write_text(
        f'command="HOME={shlex.quote(str(pathlib.Path.home()))} {locki_bin} safe-cmd",no-port-forwarding,no-X11-forwarding,no-agent-forwarding '
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
            " ".join([
                "sudo",
                "incus",
                "exec",
                shlex.quote(wt_id),
                "--cwd",
                shlex.quote(str(wt_path)),
                *(f"--env={env}=${env}" for env in forwarded_env),
                "--",
                *((shlex.quote(a) for a in ctx.args) if ctx.args else ["bash"]),
            ]),
        ],
    )
