import importlib.resources
import os
import pathlib
import tempfile

HOME = pathlib.Path.home().resolve()
XDG_CONFIG = (
    pathlib.Path(
        os.environ.get("XDG_CONFIG_HOME")
        or (os.environ.get("APPDATA") if os.name == "nt" else None)
        or (HOME / ".config")
    )
    .expanduser()
    .resolve()
)
LEGACY = HOME / ".locki"
if LEGACY.exists():
    CONFIG = DATA = STATE = RUNTIME = LEGACY
else:
    CONFIG = XDG_CONFIG / "locki"
    DATA = (
        pathlib.Path(
            os.environ.get("XDG_DATA_HOME")
            or (os.environ.get("LOCALAPPDATA") if os.name == "nt" else None)
            or (HOME / ".local" / "share")
        )
        .expanduser()
        .resolve()
        / "locki"
    )
    if xdg_state := os.environ.get("XDG_STATE_HOME"):
        STATE = pathlib.Path(xdg_state).expanduser().resolve() / "locki"
    elif os.name == "nt":
        STATE = DATA / "state"
    else:
        STATE = HOME / ".local" / "state" / "locki"
    RUNTIME = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()).expanduser().resolve() / "locki"

LIMA = STATE / "lima"
SANDBOX_HOME = DATA / "home"
WORKTREES = DATA / "worktrees"
WORKTREES_META = DATA / "worktrees-meta"
GUEST_WORKTREES = pathlib.PurePosixPath("/mnt/locki/worktrees") if os.name == "nt" else pathlib.PurePosixPath(WORKTREES)
LOG = STATE / "logs"
DENIED_LOG = STATE / "denied-commands.log"
USER_CONFIG = CONFIG / "config.toml"
PID_FILE = RUNTIME / "daemon.pid"
PORT_FILE = RUNTIME / "daemon.port"
PACKAGE_DATA = importlib.resources.files("locki") / "data"


def guest_worktree_path(
    host_path: pathlib.PurePath,
    *,
    host_root: pathlib.PurePath = WORKTREES,
    guest_root: pathlib.PurePosixPath = GUEST_WORKTREES,
) -> pathlib.PurePosixPath:
    """Translate a host worktree path to its stable POSIX path in the VM and container."""
    relative = host_path.relative_to(host_root)
    if ".." in relative.parts:
        raise ValueError(f"Path escapes worktree root: {host_path}")
    return guest_root.joinpath(*relative.parts)


def host_worktree_path(
    guest_path: pathlib.PurePosixPath | str,
    *,
    host_root: pathlib.PurePath = WORKTREES,
    guest_root: pathlib.PurePosixPath = GUEST_WORKTREES,
) -> pathlib.PurePath:
    """Translate a VM/container worktree path back to its host path."""
    relative = pathlib.PurePosixPath(guest_path).relative_to(guest_root)
    if ".." in relative.parts:
        raise ValueError(f"Path escapes worktree root: {guest_path}")
    return host_root.joinpath(*relative.parts)


class HostWorktreePathTranslator:
    """Incrementally rewrite Windows host paths in command output for the Linux guest."""

    def __init__(
        self,
        *,
        host_root: pathlib.PurePath = WORKTREES,
        guest_root: pathlib.PurePosixPath = GUEST_WORKTREES,
    ) -> None:
        roots = {str(host_root), host_root.as_posix()}
        self._patterns = tuple(
            sorted(
                {f"{root}{separator}".encode() for root in roots for separator in ("/", "\\")}, key=len, reverse=True
            )
        )
        self._folded_patterns = tuple(pattern.lower() for pattern in self._patterns)
        self._mapped = f"{guest_root}/".encode()
        self._max_pattern = max(map(len, self._patterns))
        self._pending = b""
        self._case_insensitive = isinstance(host_root, pathlib.PureWindowsPath)

    def feed(self, value: bytes, *, final: bool = False) -> bytes:
        """Translate one stream chunk, retaining any suffix which may be a split path."""
        data = self._pending + value
        comparison = data.lower() if self._case_insensitive else data
        boundary = len(data) if final else max(0, len(data) - self._max_pattern + 1)
        output = bytearray()
        offset = 0
        while offset < boundary:
            patterns = self._folded_patterns if self._case_insensitive else self._patterns
            match = next(
                (
                    pattern
                    for pattern, folded in zip(self._patterns, patterns, strict=True)
                    if comparison.startswith(folded, offset)
                ),
                None,
            )
            if match is not None:
                output.extend(self._mapped)
                offset += len(match)
            else:
                output.append(data[offset])
                offset += 1
        self._pending = data[offset:]
        return bytes(output)
