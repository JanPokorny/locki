"""`locki setup --copy` must not clobber a sandbox login that already exists (OAuth refresh tokens
rotate, so a re-copied host token forks the chain), nor MCP servers added inside the sandbox. Run: uv run python test/test_setup_copy.py"""

import json
import os
import pathlib
import tempfile
import tomllib
import types

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

# MCP servers added in the sandbox survive a re-copy; per server name the sandbox wins outright
(HOME / ".claude.json").write_text(json.dumps({"mcpServers": {"both": {"args": ["host"]}, "host": {}}, "x": 1}))
(HOME / ".codex").mkdir()
(HOME / ".codex" / "config.toml").write_text('model = "m"\n[mcp_servers.host]\ncommand = "h"\n')
(SANDBOX_HOME / ".claude.json").write_text(json.dumps({"mcpServers": {"both": {"args": ["sandbox"]}, "rea": {}}}))
(SANDBOX_HOME / ".codex").mkdir()
(SANDBOX_HOME / ".codex" / "config.toml").write_text('[mcp_servers.rea]\ncommand = "r"\nargs = ["s"]\n')
setup_cmd.main(["--copy"], standalone_mode=False)
claude = json.loads((SANDBOX_HOME / ".claude.json").read_text())
assert claude["x"] == 1 and set(claude["mcpServers"]) == {"both", "host", "rea"}, claude
assert claude["mcpServers"]["both"] == {"args": ["sandbox"]}, "sandbox entry must win whole, lists not unioned"
codex = tomllib.loads((SANDBOX_HOME / ".codex" / "config.toml").read_text())
assert codex["model"] == "m" and codex["mcp_servers"] == {
    "host": {"command": "h"},
    "rea": {"command": "r", "args": ["s"]},
}, codex

# a symlink planted at the destination must not be read: its target's servers never merge
(HOME / "secret.json").write_text(json.dumps({"mcpServers": {"leak": {}}}))
(SANDBOX_HOME / ".claude.json").unlink()
(SANDBOX_HOME / ".claude.json").symlink_to(HOME / "secret.json")
setup_cmd.main(["--copy"], standalone_mode=False)
claude = json.loads((SANDBOX_HOME / ".claude.json").read_text())
assert "leak" not in claude["mcpServers"], claude

# An unchanged file is neither backed up nor rewritten: a re-copy of an identical host must
# create no new backups at all (a home-manager hook runs this on every config change).
(HOME / ".claude" / "plugins").mkdir()
(HOME / ".claude" / "plugins" / "state.json").write_text(json.dumps({"installPath": str(HOME / "x")}))
(HOME / ".claude" / "store.md").write_text("from-store")
(HOME / ".claude" / "store.md").chmod(0o444)
os.utime(HOME / ".claude" / "store.md", (0, 0))
setup_cmd.main(["--copy"], standalone_mode=False)
assert (SANDBOX_HOME / ".claude" / "plugins" / "state.json").read_text() == json.dumps({"installPath": "/root/x"})
import locki.cmd.setup as setup_mod  # noqa: E402

clock = iter(range(10**9, 10**9 + 100))
setup_mod.time = types.SimpleNamespace(time=lambda: next(clock))  # unique backup suffix per run
before = set(SANDBOX_HOME.rglob("*.backup"))
setup_cmd.main(["--copy"], standalone_mode=False)
new_backups = [
    b.relative_to(SANDBOX_HOME).as_posix().rsplit(".", 2)[0] for b in set(SANDBOX_HOME.rglob("*.backup")) - before
]
# only the MCP-merged files legitimately differ from the host copy and get re-copied
assert set(new_backups) <= {".claude.json", ".codex/config.toml"}, new_backups
# ...but a symlinked sandbox file is always replaced, even when its target is identical
(SANDBOX_HOME / ".claude" / "CLAUDE.md").unlink()
(SANDBOX_HOME / ".claude" / "CLAUDE.md").symlink_to(HOME / ".claude" / "CLAUDE.md")
setup_cmd.main(["--copy"], standalone_mode=False)
assert not (SANDBOX_HOME / ".claude" / "CLAUDE.md").is_symlink(), "symlinked dst must be replaced by a real copy"

print("test_setup_copy: OK")
