"""`locki setup --copy` must not clobber a sandbox login that already exists (OAuth refresh tokens
rotate, so a re-copied host token forks the chain). Run: uv run python test/test_setup_copy.py"""

import os
import pathlib
import tempfile

tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp / "host")
os.environ["XDG_DATA_HOME"] = str(tmp / "data")
os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
(tmp / "host").mkdir()

from locki.cmd.setup import setup_cmd  # noqa: E402
from locki.paths import HOME, SANDBOX_HOME  # noqa: E402

(HOME / ".claude").mkdir(parents=True)
(HOME / ".claude" / ".credentials.json").write_text("host-token")
(HOME / ".claude" / "CLAUDE.md").write_text("host-instructions-v1")

setup_cmd.main(["--copy"], standalone_mode=False)
assert (SANDBOX_HOME / ".claude" / ".credentials.json").read_text() == "host-token", "first copy must seed credentials"

(SANDBOX_HOME / ".claude" / ".credentials.json").write_text("sandbox-token")
(HOME / ".claude" / "CLAUDE.md").write_text("host-instructions-v2")
setup_cmd.main(["--copy"], standalone_mode=False)
assert (SANDBOX_HOME / ".claude" / ".credentials.json").read_text() == "sandbox-token", (
    "re-copy must keep sandbox login"
)
assert (SANDBOX_HOME / ".claude" / "CLAUDE.md").read_text() == "host-instructions-v2", (
    "re-copy must still sync other files"
)
assert not list((SANDBOX_HOME / ".claude").glob(".credentials.json.*.backup")), "kept login must not be backed up"

print("test_setup_copy: OK")
