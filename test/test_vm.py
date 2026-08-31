import pathlib
import tempfile
import unittest
from unittest import mock

from locki.services import vm as vm_module


class LimaExecutableTests(unittest.TestCase):
    def test_host_memory_probe_returns_at_least_one_gibibyte(self) -> None:
        self.assertGreaterEqual(vm_module._host_memory_gib(), 1)

    def test_windows_uses_bundled_limactl_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            package_data = pathlib.Path(temp_dir)
            executable = package_data / "bin" / "limactl.exe"
            executable.parent.mkdir()
            executable.touch()

            with (
                mock.patch.object(vm_module, "PACKAGE_DATA", package_data),
                mock.patch.object(vm_module.os, "name", "nt"),
            ):
                result = vm_module.VMService().limactl

            self.assertEqual(result, str(executable))

    def test_windows_configuration_uses_linux_mount_points_and_builtin_sftp(self) -> None:
        with (
            mock.patch.object(vm_module.os, "name", "nt"),
            mock.patch.object(vm_module, "_host_memory_gib", return_value=16),
            mock.patch.object(vm_module, "nested_virt_supported", return_value=False),
            mock.patch.object(vm_module, "WORKTREES", pathlib.PureWindowsPath("C:/Users/test/Locki/worktrees")),
            mock.patch.object(vm_module, "SANDBOX_HOME", pathlib.PureWindowsPath("C:/Users/test/Locki/home")),
            mock.patch.object(vm_module, "GUEST_WORKTREES", pathlib.PurePosixPath("/mnt/locki/worktrees")),
        ):
            config = vm_module._lima_configuration("provision")

        self.assertEqual(config["minimumLimaVersion"], "2.2.0")
        self.assertEqual(config["mountType"], "reverse-sshfs")
        self.assertEqual(config["mounts"][0]["location"], "C:\\Users\\test\\Locki\\worktrees")
        self.assertEqual(config["mounts"][0]["mountPoint"], "/mnt/locki/worktrees")
        self.assertEqual(config["mounts"][1]["mountPoint"], "/root/.locki/home")
        self.assertTrue(all(mount["sshfs"] == {"sftpDriver": "builtin"} for mount in config["mounts"]))


if __name__ == "__main__":
    unittest.main()
