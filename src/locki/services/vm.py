import functools
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import typing

from locki.paths import LIMA, PACKAGE_DATA, SANDBOX_HOME, WORKTREES
from locki.utils import fail, file_lock, run_command


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
            vm_setup = (PACKAGE_DATA / "vm-setup.sh").read_text()
            lima_config = json.dumps(
                {
                    "minimumLimaVersion": "2.0.0",
                    "base": ["template:fedora"],
                    "memory": f"{os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') // (1024**3)}GiB",
                    "cpus": os.cpu_count(),
                    "disk": "200GiB",
                    "containerd": {"system": False, "user": False},
                    "mounts": [
                        {"location": str(WORKTREES), "writable": True},
                        {"location": str(SANDBOX_HOME), "mountPoint": "/root/.locki/home", "writable": True},
                    ],
                    "provision": [{"mode": "system", "script": vm_setup}],
                }
            )
            lima_fd, lima_yaml = tempfile.mkstemp(suffix=".yaml")
            try:
                os.write(lima_fd, lima_config.encode())
                os.close(lima_fd)
                run_command(
                    [self.limactl, "--tty=false", "create", lima_yaml, "--mount-writable", "--name=locki"],
                    "Preparing VM",
                    env=self.env,
                    cwd="/",
                    check=False,
                    print_success=False,
                )
            finally:
                os.unlink(lima_yaml)
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
