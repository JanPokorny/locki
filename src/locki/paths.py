import os
import pathlib

_home = pathlib.Path.home()
LEGACY = _home / ".locki"
if LEGACY.exists():
    CONFIG = DATA = STATE = RUNTIME = LEGACY
else:
    CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (_home / ".config")) / "locki"
    DATA = pathlib.Path(os.environ.get("XDG_DATA_HOME") or (_home / ".local" / "share")) / "locki"
    STATE = pathlib.Path(os.environ.get("XDG_STATE_HOME") or (_home / ".local" / "state")) / "locki"
    RUNTIME = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp") / "locki"

LIMA = STATE / "lima"
WORKTREES = DATA / "worktrees"
WORKTREES_META = DATA / "worktrees-meta"
LOG = STATE / "logs"
USER_CONFIG = CONFIG / "config.toml"

os.environ["LIMA_HOME"] = str(LIMA)  # limactl reads this; set early so every subprocess inherits it
