import os
import pathlib

HOME = pathlib.Path.home().resolve()
LEGACY = HOME / ".locki"
if LEGACY.exists():
    CONFIG = DATA = STATE = RUNTIME = LEGACY
else:
    CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config")).expanduser().resolve() / "locki"
    DATA = pathlib.Path(os.environ.get("XDG_DATA_HOME") or (HOME / ".local" / "share")).expanduser().resolve() / "locki"
    STATE = (
        pathlib.Path(os.environ.get("XDG_STATE_HOME") or (HOME / ".local" / "state")).expanduser().resolve() / "locki"
    )
    RUNTIME = (
        pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp").expanduser().resolve()
        / "locki"
    )

LIMA = STATE / "lima"
WORKTREES = DATA / "worktrees"
WORKTREES_META = DATA / "worktrees-meta"
LOG = STATE / "logs"
DENIED_LOG = STATE / "denied-commands.log"
USER_CONFIG = CONFIG / "config.toml"
PID_FILE = RUNTIME / "daemon.pid"
PORT_FILE = RUNTIME / "daemon.port"
