import importlib.resources
import os
import pathlib

HOME = pathlib.Path.home().resolve()
XDG_CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config")).expanduser().resolve()
LEGACY = HOME / ".locki"
if LEGACY.exists():
    CONFIG = DATA = STATE = RUNTIME = LEGACY
else:
    CONFIG = XDG_CONFIG / "locki"
    DATA = pathlib.Path(os.environ.get("XDG_DATA_HOME") or (HOME / ".local" / "share")).expanduser().resolve() / "locki"
    STATE = (
        pathlib.Path(os.environ.get("XDG_STATE_HOME") or (HOME / ".local" / "state")).expanduser().resolve() / "locki"
    )
    RUNTIME = (
        pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp").expanduser().resolve()
        / "locki"
    )

LIMA = STATE / "lima"
SANDBOX_HOME = DATA / "home"
WORKTREES = DATA / "worktrees"
WORKTREES_META = DATA / "worktrees-meta"
LOG = STATE / "logs"
DENIED_LOG = STATE / "denied-commands.log"
USER_CONFIG = CONFIG / "config.toml"
PID_FILE = RUNTIME / "daemon.pid"
PORT_FILE = RUNTIME / "daemon.port"
PACKAGE_DATA = importlib.resources.files("locki") / "data"
