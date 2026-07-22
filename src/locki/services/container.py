import base64
import hashlib
import pathlib
import shlex
import subprocess
import typing

from locki.config import load_config
from locki.paths import PACKAGE_DATA, WORKTREES
from locki.services.vm import vm
from locki.services.worktree import WorktreeInfo
from locki.utils import fail, file_lock

# The incus disk device that mounts the worktree; cleanup reads its source to map a container to its worktree.
WORKTREE_DEVICE = "worktree"


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
            "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true",
            "GOCACHE": "/var/cache/locki/go/build",
            "GOMODCACHE": "/var/cache/locki/go/mod",
            "GRADLE_USER_HOME": "/var/cache/locki/gradle",
            "HEX_HOME": "/var/cache/locki/hex",
            "IS_SANDBOX": "1",
            "JULIA_DEPOT_PATH": "/var/cache/locki/julia",
            "LEIN_HOME": "/var/cache/locki/lein",
            "LOCKI_SANDBOX_ID": worktree.wt_id,
            "LOCKI_WORKTREES_HOME": str(WORKTREES),
            "MAVEN_OPTS": "-Dmaven.repo.local=/var/cache/locki/maven",
            "MISE_CACHE_DIR": "/var/cache/locki/mise",
            "MISE_DATA_DIR": "/usr/share/mise",
            "MISE_GLOBAL_CONFIG_FILE": "/opt/locki/mise.toml",
            "MISE_INSTALL_PATH": "/usr/local/bin/mise",
            "MISE_NODE_VERIFY": "false",
            "MISE_TRUSTED_CONFIG_PATHS": "/",
            "MIX_HOME": "/var/cache/locki/mix",
            "NIMBLE_DIR": "/var/cache/locki/nimble",
            "npm_config_cache": "/var/cache/locki/npm",
            "NUGET_PACKAGES": "/var/cache/locki/nuget",
            "PATH": "/opt/locki/bin/high:/root/.local/bin:/usr/share/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/locki/bin/low",
            "PIP_CACHE_DIR": "/var/cache/locki/pip",
            "POETRY_VIRTUALENVS_PATH": f"/var/cache/locki/scoped/{worktree.wt_id}/poetry-venvs",
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
                stderr = result.stderr.decode()
                if "fingerprint" not in stderr:
                    fail(f"Importing container image failed: {stderr.strip()}")
                # Same content already imported under another alias (e.g. from another clone
                # of the repo). The incus fingerprint is the sha256 of the archive (metadata
                # then rootfs for split images) — compute it and add our alias to that image.
                digest = hashlib.sha256()
                for src in sources:
                    with open(src, "rb") as f:
                        while chunk := f.read(1 << 20):
                            digest.update(chunk)
                vm.run(
                    ["incus", "image", "alias", "create", alias, digest.hexdigest()[:12]],
                    "Aliasing existing image",
                    print_success=False,
                )
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
            result = vm.run(
                ["incus", "list", "--format=csv", "--columns=ns", worktree.wt_id],
                "Checking container",
                check=False,
                print_success=False,
            )
            listing = result.stdout.decode()
            if worktree.wt_id in listing:
                if "RUNNING" not in listing:
                    vm.run(
                        ["incus", "start", worktree.wt_id],
                        "Starting container",
                        check=False,
                    )
            else:
                incus_image = config.get_incus_image(worktree.repo)

                local_path = worktree.repo / incus_image
                with file_lock("image", "Waiting for another image import"):
                    image_ref = self._import_local_image(local_path) if local_path.is_file() else incus_image

                    vm.run(
                        ["incus", "init", image_ref, worktree.wt_id],
                        "Creating container",
                        print_success=False,
                    )

                vm.run(
                    [
                        "incus",
                        "config",
                        "device",
                        "add",
                        worktree.wt_id,
                        WORKTREE_DEVICE,
                        "disk",
                        f"source={worktree.wt_path}",
                        f"path={worktree.wt_path}",
                    ],
                    "Mounting worktree into container",
                    print_success=False,
                )

                vm.run(
                    ["incus", "start", worktree.wt_id],
                    "Starting container",
                )

                setup_script = (
                    (PACKAGE_DATA / "container-setup.sh")
                    .read_bytes()
                    .replace(b"__AGENTS_MD_B64__", base64.b64encode((PACKAGE_DATA / "AGENTS.md").read_bytes()))
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

    def remove(self, wt_id: str) -> None:
        """Delete the container and the sandbox-scoped cache folder in one VM roundtrip."""
        quoted = shlex.quote(wt_id)
        vm.run(
            [
                "sh",
                "-c",
                f"incus delete --force {quoted}; rm -rf /var/cache/locki/scoped/{quoted}",
            ],
            "Removing container",
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
                        shlex.quote(str(worktree.wt_path)),
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
