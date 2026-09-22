import base64
import dataclasses
import datetime
import hashlib
import json
import pathlib
import shlex
import subprocess
import typing

import click

from locki.config import load_config
from locki.paths import PACKAGE_DATA, WORKTREES
from locki.runes import INFO
from locki.services.daemon import VERSION
from locki.services.vm import INTERCEPTED_HOSTS, vm
from locki.services.worktree import WorktreeInfo
from locki.utils import fail, file_lock

# The incus disk device that mounts the worktree; cleanup reads its source to map a container to its worktree.
WORKTREE_DEVICE = "worktree"

# Root of the per-sandbox cache folders (shared caches live in /var/cache/locki directly);
# shims reach their sandbox's folder via the LOCKI_SCOPED_CACHE env var.
SCOPED_CACHE = "/var/cache/locki/scoped"

# Repo templates are stopped, device-less containers named after a hash of the repo path;
# new sandboxes of the repo start as copies of them (a btrfs snapshot, so near-instant).
TEMPLATE_PREFIX = "locki-template-"
_TEMPLATE_KEY = "user.locki-template."


@dataclasses.dataclass
class TemplateInfo:
    name: str  # incus container name
    repo: str
    source: str  # sandbox id the template was taken from
    branch: str  # that sandbox's branch at the time
    created: str  # ISO timestamp
    version: str  # locki version that took it

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def template_name(repo: pathlib.Path) -> str:
    return TEMPLATE_PREFIX + hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]


# Runs in the VM. Snapshot first so a running source yields a crash-consistent copy, and
# build under a temporary name so a failure leaves any previous template intact. The
# copy loses the sandbox's own devices (worktree mount, port forwards; orphan cleanup
# would otherwise delete it with the source worktree) and its machine-id (copies would
# share it, and with it their DHCP client id), so each copy boots as a fresh machine.
_TEMPLATE_SET_SCRIPT = r"""
set -eu
src=__SRC__ tpl=__TPL__ snap=locki-template
tmp="$tpl-new"
incus info "$src" >/dev/null 2>&1 || { echo "Sandbox $src has no container." >&2; exit 3; }
incus snapshot delete "$src" "$snap" 2>/dev/null || true
incus delete --force "$tmp" 2>/dev/null || true
incus snapshot create "$src" "$snap"
trap 'incus snapshot delete "$src" "$snap" 2>/dev/null || true' EXIT
incus copy "$src/$snap" "$tmp"
for dev in $(incus config device list "$tmp"); do incus config device remove "$tmp" "$dev" >/dev/null; done
empty=$(mktemp)
incus file push --mode=0444 "$empty" "$tmp/etc/machine-id"
rm -f "$empty"
incus file delete "$tmp/tmp/.locki-branch-named" 2>/dev/null || true
incus config set "$tmp" __KEYS__
incus delete --force "$tpl" 2>/dev/null || true
incus rename "$tmp" "$tpl"
"""


class ContainerService:
    """Per-sandbox Incus containers inside the Locki VM."""

    forwarded_env: typing.ClassVar = {"TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LANG", "SSH_TTY"}

    def env(self, worktree: WorktreeInfo) -> dict[str, str]:
        """Environment for processes running in the sandbox container.

        Shared caches live directly under /var/cache/locki; caches that cannot be
        shared across sandboxes go under /var/cache/locki/scoped/<wt-id>/ so removal
        and pruning can delete the whole folder without knowing the individual cache
        types."""
        return {
            # agy self-updates in the background; mise owns its install path here
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
            "BUN_INSTALL_CACHE_DIR": "/var/cache/locki/bun",
            "BUNDLE_PATH": "/var/cache/locki/bundle",
            "CABAL_DIR": "/var/cache/locki/cabal",
            "CARGO_HOME": "/var/cache/locki/cargo",
            "COMPOSER_CACHE_DIR": "/var/cache/locki/composer",
            "CONAN_USER_HOME": "/var/cache/locki/conan",
            "CONAN_HOME": "/var/cache/locki/conan2",
            "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/etc/copilot",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "COURSIER_CACHE": "/var/cache/locki/coursier",
            "DENO_DIR": "/var/cache/locki/deno",
            "GOCACHE": "/var/cache/locki/go/build",
            "GOMODCACHE": "/var/cache/locki/go/mod",
            "GRADLE_USER_HOME": "/var/cache/locki/gradle",
            "HEX_HOME": "/var/cache/locki/hex",
            "IS_SANDBOX": "1",
            "JULIA_DEPOT_PATH": "/var/cache/locki/julia",
            "LEIN_HOME": "/var/cache/locki/lein",
            "LOCKI_SANDBOX_ID": worktree.wt_id,
            "LOCKI_SCOPED_CACHE": f"{SCOPED_CACHE}/{worktree.wt_id}",
            "LOCKI_WORKTREES_HOME": str(WORKTREES),
            "MAVEN_OPTS": "-Dmaven.repo.local=/var/cache/locki/maven",
            "MISE_CACHE_DIR": "/var/cache/locki/mise",
            "MISE_DATA_DIR": "/usr/share/mise",
            "MISE_GLOBAL_CONFIG_FILE": "/opt/locki/mise.toml",
            "MISE_INSTALL_PATH": "/usr/local/bin/mise",
            "MISE_NODE_VERIFY": "false",
            # Provenance stays on, but an unreachable/rate-limited api.github.com must not be
            # fatal -- that is exactly when the lockfile fallback runs, and checksums still hold.
            "MISE_PROVENANCE_API_FAILURES_FATAL": "false",
            "MISE_TRUSTED_CONFIG_PATHS": "/",
            "MIX_HOME": "/var/cache/locki/mix",
            "NIMBLE_DIR": "/var/cache/locki/nimble",
            "npm_config_cache": "/var/cache/locki/npm",
            "NUGET_PACKAGES": "/var/cache/locki/nuget",
            "PATH": "/opt/locki/bin/high:/root/.local/bin:/usr/share/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/locki/bin/low",
            "PIP_CACHE_DIR": "/var/cache/locki/pip",
            "POETRY_VIRTUALENVS_PATH": f"{SCOPED_CACHE}/{worktree.wt_id}/poetry-venvs",
            "POETRY_VIRTUALENVS_IN_PROJECT": "false",
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

    def _import_local_image(self, local_path: pathlib.Path) -> str:
        """Copy a local Incus image archive into the VM and import it, cached by file identity.

        Supports both unified tarballs and split images (metadata + companion .root file).
        The alias encodes the archive path and the size/mtime of its file(s), so repeat
        sandbox creations from an unchanged archive skip the copy+import entirely, and a
        changed archive replaces the previously imported image."""
        sources = [src for suffix in ("", ".root") if (src := local_path.parent / (local_path.name + suffix)).is_file()]
        path_hash = hashlib.sha256(str(local_path.resolve()).encode()).hexdigest()[:8]
        signature = "|".join(f"{src.stat().st_size}:{src.stat().st_mtime_ns}" for src in sources)
        alias = f"locki-img-{path_hash}-{hashlib.sha256(signature.encode()).hexdigest()[:8]}"

        result = vm.run(
            ["incus", "image", "list", "--format=csv", "--columns=l"],
            "Checking for cached image",
            check=False,
            quiet=True,
        )
        cached_aliases = result.stdout.decode().split()
        if alias in cached_aliases:
            return alias
        for stale in cached_aliases:
            if stale.startswith(f"locki-img-{path_hash}-"):
                vm.run(
                    ["incus", "image", "delete", stale],
                    "Removing stale cached image",
                    check=False,
                    quiet=True,
                )

        vm_files = []
        for src in sources:
            suffix = ".root" if src != local_path else ""
            vm_path = f"/tmp/{alias}{suffix}"
            vm.copy_into(src.resolve(), vm_path, f"Copying {'rootfs' if suffix else 'image'} into VM")
            vm_files.append(vm_path)
        try:
            result = vm.run(
                ["incus", "image", "import", *vm_files, f"--alias={alias}"],
                "Importing container image",
                check=False,
                print_success=False,
            )
            if result.returncode != 0:
                # Possibly the same content already imported under another alias (e.g. from
                # another clone of the repo). The incus fingerprint is the sha256 of the
                # archive (metadata then rootfs for split images) — try aliasing it, and only
                # if that also fails report the original import error.
                digest = hashlib.sha256()
                for src in sources:
                    with open(src, "rb") as f:
                        while chunk := f.read(1 << 20):
                            digest.update(chunk)
                aliased = vm.run(
                    ["incus", "image", "alias", "create", alias, digest.hexdigest()[:12]],
                    "Aliasing existing image",
                    check=False,
                    print_success=False,
                )
                if aliased.returncode != 0:
                    fail(f"Importing container image failed: {result.stderr.decode().strip()}")
        finally:
            vm.run(
                ["rm", "-f", *vm_files],
                "Cleaning up copied image archive",
                check=False,
                quiet=True,
                print_success=False,
            )
        return alias

    def ensure_running(self, worktree: WorktreeInfo) -> None:
        """Create (importing the image if needed), configure, and start the sandbox's container."""
        config = load_config(worktree.repo)

        with file_lock(f"provision-{worktree.wt_id}", "Waiting for another sandbox setup"):
            wt_id_q = shlex.quote(worktree.wt_id)
            # One roundtrip for the hot path: start it if it exists (a no-op error when
            # already running), then report whether it exists at all.
            result = vm.run(
                ["sh", "-c", f"incus start {wt_id_q} 2>/dev/null; incus list --format=csv --columns=n {wt_id_q}"],
                "Checking container",
                check=False,
                print_success=False,
            )
            if worktree.wt_id not in result.stdout.decode() and (template := self.template(worktree.repo)):
                self._create_from_template(worktree, template)
            elif worktree.wt_id not in result.stdout.decode():
                incus_image = config.get_incus_image(worktree.repo)

                local_path = worktree.repo / incus_image
                with file_lock("image", "Waiting for another image import"):
                    image_ref = self._import_local_image(local_path) if local_path.is_file() else incus_image

                    wt_path_q = shlex.quote(str(worktree.path))
                    vm.run(
                        [
                            "sh",
                            "-c",
                            " && ".join(
                                [
                                    f"incus init {shlex.quote(image_ref)} {wt_id_q}",
                                    f"incus config device add {wt_id_q} {WORKTREE_DEVICE} disk"
                                    f" source={wt_path_q} path={wt_path_q}",
                                    f"incus start {wt_id_q}",
                                ]
                            ),
                        ],
                        "Starting container",
                    )

                setup_script = (
                    (PACKAGE_DATA / "container-setup.sh")
                    .read_bytes()
                    .replace(b"__INTERCEPTED_HOSTS__", " ".join(INTERCEPTED_HOSTS).encode())
                    .replace(b"__AGENTS_MD_B64__", base64.b64encode((PACKAGE_DATA / "AGENTS.md").read_bytes()))
                    .replace(b"__MISE_LOCK_B64__", base64.b64encode((PACKAGE_DATA / "mise.lock").read_bytes()))
                    .replace(
                        b"__LIBATOMIC_B64__",
                        base64.b64encode((PACKAGE_DATA / "libatomic.so.1").read_bytes())
                        if (PACKAGE_DATA / "libatomic.so.1").is_file()
                        else b"",
                    )
                )
                env_flags = [flag for k, v in self.env(worktree).items() for flag in ("--env", f"{k}={v}")]
                vm.run(
                    [
                        "incus",
                        "exec",
                        worktree.wt_id,
                        *env_flags,
                        "--",
                        "/bin/sh",
                    ],
                    "Configuring container",
                    input=setup_script,
                    print_success=False,
                )

    def _create_from_template(self, worktree: WorktreeInfo, template: TemplateInfo) -> None:
        """Create the sandbox container as a copy of its repo's template.  The template
        was fully set up already, so container-setup.sh (not idempotent) is skipped;
        unsetting `volatile.apply_template` keeps the image's copy-time templates from
        regenerating files the setup customized (e.g. /etc/hosts)."""
        if template.version != VERSION:
            click.echo(
                f"{INFO} This repo's sandbox template was taken with Locki {template.version} (now {VERSION});"
                f" re-run {click.style('locki template set', fg='green')} to pick up sandbox setup changes.",
                err=True,
            )
        wt_id_q = shlex.quote(worktree.wt_id)
        tpl_q = shlex.quote(template.name)
        wt_path_q = shlex.quote(str(worktree.path))
        with file_lock(f"template-{template.name}", "Waiting for the sandbox template to update"):
            vm.run(
                [
                    "sh",
                    "-c",
                    " && ".join(
                        [
                            f"incus copy {tpl_q} {wt_id_q}",
                            f"{{ incus config unset {wt_id_q} volatile.apply_template 2>/dev/null || true; }}",
                            f"incus config device add {wt_id_q} {WORKTREE_DEVICE} disk"
                            f" source={wt_path_q} path={wt_path_q}",
                            f"incus start {wt_id_q}",
                        ]
                    ),
                ],
                f"Starting container from template of {click.style(template.source, fg='green')}",
            )

    def template(self, repo: pathlib.Path) -> TemplateInfo | None:
        """The template new sandboxes of *repo* are copied from, if one is set."""
        if vm.status() is None:
            return None  # no VM, so no templates
        name = template_name(repo)
        result = vm.run(
            ["incus", "query", f"/1.0/instances/{name}"],
            "Checking for sandbox template",
            check=False,
            quiet=True,
        )
        if result.returncode != 0:
            return None
        try:
            config = json.loads(result.stdout).get("config") or {}
        except json.JSONDecodeError:
            return None
        return TemplateInfo(
            name=name,
            **{
                f.name: config.get(_TEMPLATE_KEY + f.name, "")
                for f in dataclasses.fields(TemplateInfo)
                if f.name != "name"
            },
        )

    def set_template(self, worktree: WorktreeInfo) -> TemplateInfo:
        """Make a copy of *worktree*'s container the template for its repo's new sandboxes,
        replacing any previous one."""
        name = template_name(worktree.repo)
        info = TemplateInfo(
            name=name,
            repo=str(worktree.repo),
            source=worktree.wt_id,
            branch=worktree.branch,
            created=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            version=VERSION,
        )
        keys = " ".join(shlex.quote(f"{_TEMPLATE_KEY}{k}={v}") for k, v in info.as_dict().items() if k != "name")
        script = (
            _TEMPLATE_SET_SCRIPT.replace("__SRC__", shlex.quote(worktree.wt_id))
            .replace("__TPL__", shlex.quote(name))
            .replace("__KEYS__", keys)
        )
        with file_lock(f"template-{name}", "Waiting for another sandbox template update"):
            result = vm.run(["sh", "-c", script], "Saving sandbox template", check=False)
        if result.returncode == 3:
            fail(f"Sandbox {worktree.wt_id} has no container yet. Enter it first, e.g. `locki x -m {worktree.wt_id}`.")
        if result.returncode != 0:
            fail(f"Saving sandbox template failed: {result.stderr.decode(errors='replace').strip()}")
        return info

    def unset_template(self, repo: pathlib.Path) -> TemplateInfo | None:
        """Delete *repo*'s template; returns what was removed (None if nothing was set)."""
        info = self.template(repo)
        if info is None:
            return None
        with file_lock(f"template-{info.name}", "Waiting for another sandbox template update"):
            vm.run(["incus", "delete", "--force", info.name], "Removing sandbox template")
        return info

    def remove(self, *wt_ids: str) -> None:
        """Delete container(s) and their sandbox-scoped cache folders in one VM roundtrip."""
        if not wt_ids:
            return
        script = "; ".join(f"incus delete --force {q}; rm -rf {SCOPED_CACHE}/{q}" for q in map(shlex.quote, wt_ids))
        vm.run(
            ["sh", "-c", script],
            "Removing containers" if len(wt_ids) > 1 else "Removing container",
            check=False,
        )

    def exec_interactive(self, worktree: WorktreeInfo, command: list[str]) -> subprocess.CompletedProcess:
        """Run *command* in the sandbox container with inherited stdio."""
        return vm.shell(
            [
                "bash",
                "-c",
                " ".join(
                    [
                        "sudo",
                        "incus",
                        "exec",
                        shlex.quote(worktree.wt_id),
                        "--cwd",
                        shlex.quote(str(worktree.path)),
                        *(f"--env={k}={v}" for k, v in self.env(worktree).items()),
                        *(f'--env={env}="${env}"' for env in self.forwarded_env),
                        "--",
                        *(shlex.quote(a) for a in command),
                    ]
                ),
            ],
            self.forwarded_env,
        )


containers = ContainerService()
