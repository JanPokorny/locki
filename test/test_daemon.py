import contextlib
import pathlib
import tempfile
import unittest
from unittest import mock

from locki.services import daemon as daemon_module


class DaemonServiceTests(unittest.TestCase):
    def test_windows_daemon_is_started_with_detached_process_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            runtime = root / "runtime"
            sandbox_home = root / "home"
            runtime.mkdir()
            pid_file = runtime / "daemon.pid"
            port_file = runtime / "daemon.port"
            version_file = runtime / "daemon.version"

            def start_daemon(*_args, **_kwargs) -> mock.Mock:
                port_file.write_text("4567")
                version_file.write_text(daemon_module.VERSION)
                return mock.Mock()

            with (
                mock.patch.multiple(
                    daemon_module,
                    RUNTIME=runtime,
                    SANDBOX_HOME=sandbox_home,
                    PID_FILE=pid_file,
                    PORT_FILE=port_file,
                    VERSION_FILE=version_file,
                ),
                mock.patch.object(daemon_module, "file_lock", return_value=contextlib.nullcontext()),
                mock.patch.object(daemon_module.os, "name", "nt"),
                mock.patch.object(daemon_module.subprocess, "Popen", side_effect=start_daemon) as popen,
            ):
                daemon_module.DaemonService().ensure_running()

            self.assertEqual(popen.call_args.kwargs["creationflags"], 0x00000208)
            self.assertNotIn("start_new_session", popen.call_args.kwargs)

    def test_running_current_version_daemon_is_reused_without_signaling_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            runtime = root / "runtime"
            sandbox_home = root / "home"
            runtime.mkdir()
            pid_file = runtime / "daemon.pid"
            port_file = runtime / "daemon.port"
            version_file = runtime / "daemon.version"
            pid_file.write_text("123")
            port_file.write_text("4567")
            version_file.write_text(daemon_module.VERSION)

            with (
                mock.patch.multiple(
                    daemon_module,
                    RUNTIME=runtime,
                    SANDBOX_HOME=sandbox_home,
                    PID_FILE=pid_file,
                    PORT_FILE=port_file,
                    VERSION_FILE=version_file,
                ),
                mock.patch.object(daemon_module, "file_lock", return_value=contextlib.nullcontext()),
                mock.patch.object(daemon_module, "process_is_running", return_value=True) as is_running,
                mock.patch.object(daemon_module.subprocess, "Popen") as popen,
            ):
                daemon_module.DaemonService().ensure_running()

            is_running.assert_called_once_with(123)
            popen.assert_not_called()
            self.assertIn("Port 4567", (sandbox_home / ".ssh" / "locki-ssh-config").read_text())


if __name__ == "__main__":
    unittest.main()
