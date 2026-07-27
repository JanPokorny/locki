import functools
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import typing

from locki.paths import LIMA, PACKAGE_DATA, SANDBOX_HOME, WORKTREES
from locki.utils import fail, file_lock, run_command

# Where the shared sandbox home lands inside the VM: the Lima mount point and the
# source of the incus profile's `home` device (substituted into vm-setup.sh).
SANDBOX_HOME_MOUNT = "/root/.locki/home"

# Hosts hijacked in every sandbox's /etc/hosts and TLS-terminated by the VM's nginx
# cache, grouped by nginx server block (each group has its own caching rules).
REGISTRY_HOSTS = [
    "registry-1.docker.io",
    "mirror.gcr.io",
    "ghcr.io",
    "gcr.io",
    "quay.io",
    "registry.access.redhat.com",
    "registry.k8s.io",
    "public.ecr.aws",
    "cgr.dev",
    "nvcr.io",
    "registry.gitlab.com",
]
K3S_HOSTS = ["get.k3s.io"]
GH_ASSET_HOSTS = ["objects.githubusercontent.com", "release-assets.githubusercontent.com"]
INTERCEPTED_HOSTS = [*REGISTRY_HOSTS, *K3S_HOSTS, *GH_ASSET_HOSTS]


def nested_virt_supported() -> bool:
    """Nested virt needs vz (macOS 15+, M3+); Lima's qemu driver ignores the field, and on
    Linux the guest inherits it from host KVM via -cpu host anyway, so no field needed there."""
    if sys.platform != "darwin":
        return False
    # naive heuristic: parses "Apple M<n>" from the brand string; the real probe is
    # vz's IsNestedVirtualizationSupported, which Lima only calls at VM start
    brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout
    chip = re.search(r"Apple M(\d+)", brand)
    return int(platform.mac_ver()[0].split(".")[0]) >= 15 and chip is not None and int(chip[1]) >= 3


class VMService:
    """All interaction with the Lima VM ("locki") that hosts the sandbox containers."""

    env: typing.ClassVar = {"LIMA_HOME": str(LIMA)}

    @functools.cached_property
    def limactl(self) -> str:
        bundled = PACKAGE_DATA / "bin" / "limactl"
        if bundled.is_file():
            return str(bundled)
        system = shutil.which("limactl")
        if system:
            return system
        fail("limactl is not installed. Please install Lima or use a platform-specific locki wheel.")

    def status(self) -> str | None:
        """Return the Locki VM status ('Running', 'Stopped', etc.), or None."""
        result = subprocess.run(
            [self.limactl, "list", "locki", "--format", "{{.Status}}"],
            capture_output=True,
            text=True,
            env={**os.environ, **self.env},
        )
        return result.stdout.strip() or None

    def run(
        self,
        command: list[str],
        message: str,
        env: dict[str, str] | None = None,
        input: bytes | None = None,
        check: bool = True,
        quiet: bool = False,
        print_success: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a command in the VM as root, starting the VM if needed."""
        return run_command(
            [self.limactl, "shell", "--start", "--preserve-env", "--tty=false", "locki", "--", "sudo", "-E", *command],
            message,
            env={**self.env, **(env or {})},
            cwd="/",
            input=input,
            check=check,
            quiet=quiet,
            print_success=print_success,
        )

    def incus(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run incus in the VM without starting it and without a spinner (daemon-safe)."""
        return subprocess.run(
            [self.limactl, "shell", "--tty=false", "locki", "--", "sudo", "incus", *args],
            capture_output=True,
            text=True,
            env={**os.environ, **self.env},
        )

    def copy_into(self, src: pathlib.Path, vm_path: str, message: str) -> None:
        run_command(
            [self.limactl, "copy", str(src), f"locki:{vm_path}"],
            message,
            env=self.env,
            cwd="/",
            print_success=False,
        )

    def shell(self, command: list[str], forward_env: set[str]) -> subprocess.CompletedProcess:
        """Interactive shell into the VM with inherited stdio, starting the VM if needed."""
        return subprocess.run(
            [self.limactl, "shell", "--yes", "--preserve-env", "--start", "--workdir=/", "locki", "--", *command],
            env={**os.environ, **self.env, "LIMA_SHELLENV_ALLOW": ",".join(forward_env)},
        )

    def ensure_running(self) -> None:
        """Create the VM if needed and start it, unless it is already running."""
        if sys.platform == "linux" and (
            missing := [b for b in [f"qemu-system-{platform.machine()}", "qemu-img"] if not shutil.which(b)]
        ):
            fail(
                f"Locki requires QEMU on Linux, but {', '.join(missing)} not found in PATH. Install QEMU: https://www.qemu.org/download/#linux"
            )

        if self.status() == "Running":
            return

        LIMA.mkdir(exist_ok=True, parents=True)
        with file_lock("vm", "Waiting for VM to start"):
            vm_setup = (
                (PACKAGE_DATA / "vm-setup.sh")
                .read_text()
                .replace("__SANS__", ",".join(f"DNS:{h}" for h in [*INTERCEPTED_HOSTS, "docker-io.locki"]))
                .replace("__REGISTRY_HOSTS__", " ".join(REGISTRY_HOSTS))
                .replace("__K3S_HOSTS__", " ".join(K3S_HOSTS))
                .replace("__GH_ASSET_HOSTS__", " ".join(GH_ASSET_HOSTS))
                .replace("__SANDBOX_HOME_MOUNT__", SANDBOX_HOME_MOUNT)
            )
            lima_config = json.dumps(
                {
                    "minimumLimaVersion": "2.0.0",
                    "base": ["template:fedora"],
                    "memory": f"{os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') // (1024**3)}GiB",
                    "cpus": os.cpu_count(),
                    "disk": "200GiB",
                    "containerd": {"system": False, "user": False},
                    **({"nestedVirtualization": True} if nested_virt_supported() else {}),
                    "mounts": [
                        {"location": str(WORKTREES), "writable": True},
                        {"location": str(SANDBOX_HOME), "mountPoint": SANDBOX_HOME_MOUNT, "writable": True},
                    ],
                    "provision": [{"mode": "system", "script": vm_setup}],
                }
            )
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as lima_yaml:
                lima_yaml.write(lima_config)
            try:
                run_command(
                    [self.limactl, "--tty=false", "create", lima_yaml.name, "--mount-writable", "--name=locki"],
                    "Preparing VM",
                    env=self.env,
                    cwd="/",
                    check=False,
                    print_success=False,
                )
            finally:
                os.unlink(lima_yaml.name)
            run_command(
                [self.limactl, "--tty=false", "start", "locki"],
                "Starting VM",
                env=self.env,
                cwd="/",
                check=False,
            )

        if self.status() != "Running":
            fail(f"Lima VM failed to start. LIMA_HOME={LIMA}")

    def stop(self, force: bool = True, check: bool = True, quiet: bool = False) -> None:
        run_command(
            [self.limactl, "stop", *(["-f"] if force else []), "locki"],
            "Stopping VM",
            env=self.env,
            cwd="/",
            check=check,
            quiet=quiet,
        )

    def delete(self) -> None:
        run_command(
            [self.limactl, "delete", "-f", "locki"],
            "Deleting VM",
            env=self.env,
            cwd="/",
        )


vm = VMService()
