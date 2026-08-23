import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from locki.utils import file_lock, process_is_running


class ProcessTests(unittest.TestCase):
    def test_current_process_is_running(self) -> None:
        self.assertTrue(process_is_running(os.getpid()))

    def test_unknown_process_is_not_running(self) -> None:
        self.assertFalse(process_is_running(2**30))

    def test_inaccessible_posix_process_does_not_reuse_an_untrusted_pid(self) -> None:
        with mock.patch("locki.utils.os.kill", side_effect=PermissionError):
            self.assertFalse(process_is_running(123))


class FileLockTests(unittest.TestCase):
    def test_lock_serializes_competing_threads(self) -> None:
        entered = threading.Event()
        finished = threading.Event()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch("locki.utils.RUNTIME", pathlib.Path(temp_dir)):

            def compete() -> None:
                with file_lock("shared", "Waiting for test lock"):
                    entered.set()
                finished.set()

            with file_lock("shared", "Waiting for test lock"):
                thread = threading.Thread(target=compete)
                thread.start()
                self.assertFalse(entered.wait(0.1))

            self.assertTrue(entered.wait(2))
            self.assertTrue(finished.wait(2))
            thread.join()


if __name__ == "__main__":
    unittest.main()
