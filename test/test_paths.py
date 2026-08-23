import pathlib
import unittest

from locki.paths import HostWorktreePathTranslator, guest_worktree_path, host_worktree_path


class WorktreePathMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host_root = pathlib.PureWindowsPath(r"C:\Users\Ada\AppData\Local\locki\worktrees")
        self.guest_root = pathlib.PurePosixPath("/mnt/locki/worktrees")

    def test_windows_host_path_maps_to_linux_guest_path(self) -> None:
        host_path = self.host_root / "demo-locki-a1b2c3d4" / "src"

        result = guest_worktree_path(host_path, host_root=self.host_root, guest_root=self.guest_root)

        self.assertEqual(result, pathlib.PurePosixPath("/mnt/locki/worktrees/demo-locki-a1b2c3d4/src"))

    def test_linux_guest_path_maps_back_to_windows_host_path(self) -> None:
        guest_path = self.guest_root / "demo-locki-a1b2c3d4" / "src"

        result = host_worktree_path(guest_path, host_root=self.host_root, guest_root=self.guest_root)

        self.assertEqual(
            result,
            pathlib.PureWindowsPath(r"C:\Users\Ada\AppData\Local\locki\worktrees\demo-locki-a1b2c3d4\src"),
        )

    def test_path_outside_mapped_root_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            guest_worktree_path(
                pathlib.PureWindowsPath(r"C:\Users\Ada\Documents\secret.txt"),
                host_root=self.host_root,
                guest_root=self.guest_root,
            )

    def test_guest_path_cannot_escape_mapped_root(self) -> None:
        with self.assertRaises(ValueError):
            host_worktree_path(
                self.guest_root / ".." / "secret.txt",
                host_root=self.host_root,
                guest_root=self.guest_root,
            )

    def test_streamed_host_paths_are_translated_across_every_chunk_boundary(self) -> None:
        output = b"root=C:/Users/Ada/AppData/Local/locki/worktrees/demo-locki-a1b2c3d4\n"
        expected = b"root=/mnt/locki/worktrees/demo-locki-a1b2c3d4\n"

        for split in range(len(output) + 1):
            translator = HostWorktreePathTranslator(host_root=self.host_root, guest_root=self.guest_root)
            result = (
                translator.feed(output[:split]) + translator.feed(output[split:]) + translator.feed(b"", final=True)
            )
            self.assertEqual(result, expected, f"failed at split {split}")

    def test_windows_output_translation_is_case_insensitive(self) -> None:
        translator = HostWorktreePathTranslator(host_root=self.host_root, guest_root=self.guest_root)

        result = translator.feed(b"c:/users/ada/appdata/local/LOCKI/worktrees/demo/file", final=True)

        self.assertEqual(result, b"/mnt/locki/worktrees/demo/file")


if __name__ == "__main__":
    unittest.main()
