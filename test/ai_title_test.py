"""Self-check for HomeService.ai_title. Run: uv run python test/ai_title_test.py"""

import os
import pathlib
import sys
import tempfile

tmp = tempfile.TemporaryDirectory()
os.environ["HOME"] = tmp.name
for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
    os.environ.pop(var, None)
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from locki.services.home import home  # noqa: E402
from locki.services.worktree import WorktreeInfo  # noqa: E402

s = WorktreeInfo(wt_id="abcd1234", branch="x#locki-abcd1234", repo=pathlib.Path("/repo/proj"))
ai_title = lambda s: home.ai_title(s.path)  # noqa: E731
project = home.claude_project_dir(s.path)

assert ai_title(s) == "", "missing project dir -> empty"

project.mkdir(parents=True)
placeholder = project / "00000000-placeholder.jsonl"
placeholder.write_text("\n")
assert ai_title(s) == "", "placeholder-only transcript -> empty"

old = project / "11111111-old.jsonl"
old.write_text(
    '{"type":"ai-title","aiTitle":"First title","sessionId":"1"}\n'
    '{"type":"user"}\n'
    '{"type":"ai-title","aiTitle":"Second title","sessionId":"1"}\n'
    '{"type":"ai-title","aiTitle"'  # torn live append
)
assert ai_title(s) == "Second title", "last complete ai-title line wins"

new = project / "22222222-new.jsonl"
new.write_text('{"type":"user"}\n')
os.utime(old, (1, 1))
assert ai_title(s) == "Second title", "titleless newest file falls back to older"

new.write_text('{"type":"user"}\n{"type":"ai-title","aiTitle":"Newest title","sessionId":"2"}\n')
assert ai_title(s) == "Newest title", "newest file with a title wins"

print("ok")
