import os
import pathlib

def _normalized_path(path: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(path).resolve()


HOME = _normalized_path(pathlib.Path.home())
LEGACY = HOME / ".locki"
if LEGACY.exists():
    CONFIG = DATA = STATE = RUNTIME = LEGACY
else:
    CONFIG = _normalized_path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config")) / "locki"
    DATA = _normalized_path(os.environ.get("XDG_DATA_HOME") or (HOME / ".local" / "share")) / "locki"
    STATE = _normalized_path(os.environ.get("XDG_STATE_HOME") or (HOME / ".local" / "state")) / "locki"
    RUNTIME = _normalized_path(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp") / "locki"

LIMA = STATE / "lima"
WORKTREES = DATA / "worktrees"
WORKTREES_META = DATA / "worktrees-meta"
LOG = STATE / "logs"
DENIED_LOG = STATE / "denied-commands.log"
USER_CONFIG = CONFIG / "config.toml"
