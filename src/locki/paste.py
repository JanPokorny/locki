"""Host-side clipboard interception for sandboxed AI agents.

Most TUI AI agents (Claude Code, Codex, opencode, gemini, ...) treat ``Ctrl-V``
(byte ``0x16``) as a special keystroke that triggers a manual clipboard read
using whatever native tool is available on the host (``pbpaste`` on macOS,
``xclip``/``wl-paste`` on Linux). Inside our Fedora-based sandboxes that always
fails — there's no display server and the clipboard belongs to the host anyway,
which we can't proactively sync for security reasons.

We intercept ``0x16`` on the ``locki`` (host) side, before the byte stream
enters the VM. When we see one we read the host clipboard, persist its
contents under ``<worktree>/.locki/paste/NNN.<ext>``, and inject a textual
``[pasted file @<relpath> ]`` marker in place of the keystroke. Agents see a
plain prompt token they already know how to resolve via ``@`` file references,
and the binary blob is reachable inside the sandbox at the same path because
the worktree is bind-mounted into the container."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import pathlib
import platform
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import tty
from collections.abc import Callable

logger = logging.getLogger(__name__)

CTRL_V = 0x16

# Probed in order; first match wins.
_IMAGE_MIMES: list[tuple[str, str]] = [
    ("image/png", "png"),
    ("image/jpeg", "jpg"),
    ("image/webp", "webp"),
    ("image/gif", "gif"),
    ("image/bmp", "bmp"),
    ("image/tiff", "tiff"),
]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
    except FileNotFoundError, subprocess.TimeoutExpired, OSError:
        return None


def _read_macos_clipboard() -> tuple[bytes, str] | None:
    fd, tmppath = tempfile.mkstemp(suffix=".png", prefix="locki-paste-")
    os.close(fd)
    try:
        # AppleScript dumps a PNG-encoded clipboard to a temp file. On
        # non-image clipboards the «class PNGf» coercion fails and we hit
        # the on-error branch.
        script = (
            "try\n"
            "  set imgData to (the clipboard as «class PNGf»)\n"
            f'  set f to (open for access POSIX file "{tmppath}" with write permission)\n'
            "  set eof of f to 0\n"
            "  write imgData to f\n"
            "  close access f\n"
            '  return "ok"\n'
            "on error\n"
            "  try\n"
            f'    close access (POSIX file "{tmppath}")\n'
            "  end try\n"
            '  return "no"\n'
            "end try\n"
        )
        result = _run(["osascript", "-e", script])
        if result and result.returncode == 0 and result.stdout.strip() == b"ok":
            data = pathlib.Path(tmppath).read_bytes()
            if data:
                return data, "png"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmppath)
    text = _run(["pbpaste"])
    if text and text.returncode == 0 and text.stdout:
        return text.stdout, "txt"
    return None


def _read_wayland_clipboard() -> tuple[bytes, str] | None:
    types = _run(["wl-paste", "--list-types"])
    mimes = types.stdout.decode("utf-8", errors="ignore").split() if types else []
    for mime, ext in _IMAGE_MIMES:
        if mime in mimes:
            payload = _run(["wl-paste", "--no-newline", "--type", mime])
            if payload and payload.returncode == 0 and payload.stdout:
                return payload.stdout, ext
    payload = _run(["wl-paste", "--no-newline"])
    if payload and payload.returncode == 0 and payload.stdout:
        return payload.stdout, "txt"
    return None


def _read_x11_clipboard() -> tuple[bytes, str] | None:
    if shutil.which("xclip"):
        targets = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
        atoms = targets.stdout.decode("utf-8", errors="ignore").split() if targets else []
        for mime, ext in _IMAGE_MIMES:
            if mime in atoms:
                payload = _run(["xclip", "-selection", "clipboard", "-t", mime, "-o"])
                if payload and payload.returncode == 0 and payload.stdout:
                    return payload.stdout, ext
        payload = _run(["xclip", "-selection", "clipboard", "-o"])
        if payload and payload.returncode == 0 and payload.stdout:
            return payload.stdout, "txt"
    if shutil.which("xsel"):
        payload = _run(["xsel", "--clipboard", "--output"])
        if payload and payload.returncode == 0 and payload.stdout:
            return payload.stdout, "txt"
    return None


def read_host_clipboard() -> tuple[bytes, str] | None:
    """Read the host clipboard, preferring image data.

    Returns ``(content, extension)`` or ``None`` if the clipboard is empty
    or no clipboard tool is available.
    """
    system = platform.system()
    if system == "Darwin":
        return _read_macos_clipboard()
    if system == "Linux":
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            payload = _read_wayland_clipboard()
            if payload is not None:
                return payload
        if os.environ.get("DISPLAY"):
            return _read_x11_clipboard()
    return None


def _next_paste_seq(paste_dir: pathlib.Path) -> int:
    if not paste_dir.is_dir():
        return 1
    best = 0
    for entry in paste_dir.iterdir():
        if entry.stem.isdigit():
            best = max(best, int(entry.stem))
    return best + 1


def _save_paste(worktree_path: pathlib.Path, content: bytes, ext: str) -> pathlib.Path:
    paste_dir = worktree_path / ".locki" / "paste"
    paste_dir.mkdir(parents=True, exist_ok=True)
    seq = _next_paste_seq(paste_dir)
    out = paste_dir / f"{seq:03d}.{ext}"
    out.write_bytes(content)
    return out


def make_stdin_filter(worktree_path: pathlib.Path) -> Callable[[int], bytes]:
    """Return a stdin reader that intercepts ``Ctrl-V`` and rewrites it to a
    ``[pasted file @<relpath> ]`` reference pointing at a saved clipboard
    dump under ``<worktree>/.locki/paste/``."""

    def stdin_filter(fd: int) -> bytes:
        try:
            data = os.read(fd, 4096)
        except OSError:
            return b""
        if CTRL_V not in data:
            return data
        out = bytearray()
        for byte in data:
            if byte != CTRL_V:
                out.append(byte)
                continue
            payload = read_host_clipboard()
            if payload is None:
                # No clipboard tool, or empty clipboard — pass the keystroke
                # through so the agent's native fallback path runs.
                out.append(byte)
                continue
            try:
                path = _save_paste(worktree_path, *payload)
            except OSError as exc:
                logger.warning("Failed to save pasted clipboard: %s", exc)
                out.append(byte)
                continue
            try:
                ref = str(path.relative_to(worktree_path))
            except ValueError:
                ref = str(path)
            # Surrounding spaces keep the agent's `@`-parser from greedily
            # swallowing the closing bracket into the path.
            out.extend(f" [pasted file @{ref} ] ".encode())
        return bytes(out)

    return stdin_filter


def _propagate_winsize(master_fd: int, stdin_fd: int) -> None:
    try:
        size = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _writeall(fd: int, data: bytes) -> None:
    n = 0
    while n < len(data):
        try:
            n += os.write(fd, data[n:])
        except OSError:
            break


def spawn_with_filter(argv: list[str], stdin_filter: Callable[[int], bytes]) -> int:
    """Run ``argv`` in a PTY, applying ``stdin_filter`` to the user's stdin.

    Returns the child's exit code (negative if killed by a signal, mirroring
    ``os.waitstatus_to_exitcode``)."""
    pid, master_fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            sys.stderr.write(f"locki: failed to exec {argv[0]}: {exc}\n")
            os._exit(127)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    _propagate_winsize(master_fd, stdin_fd)

    def _on_winch(_signum, _frame):
        _propagate_winsize(master_fd, stdin_fd)

    old_winch = signal.signal(signal.SIGWINCH, _on_winch)

    try:
        old_mode = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
        restore_mode = True
    except termios.error:
        restore_mode = False

    fds = [master_fd, stdin_fd]
    try:
        while fds:
            try:
                rfds, _, _ = select.select(fds, [], [])
            except InterruptedError:
                continue
            if master_fd in rfds:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                _writeall(stdout_fd, data)
            if stdin_fd in rfds:
                try:
                    data = stdin_filter(stdin_fd)
                except OSError:
                    data = b""
                if not data:
                    fds.remove(stdin_fd)
                else:
                    _writeall(master_fd, data)
    finally:
        signal.signal(signal.SIGWINCH, old_winch)
        if restore_mode:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_mode)
        with contextlib.suppress(OSError):
            os.close(master_fd)

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)
