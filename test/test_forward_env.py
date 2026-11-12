"""`forward_env` is user-only (a repo must not pull host secrets into a sandbox) and takes only
plain variable names (they reach a shell command line). Run: uv run python test/test_forward_env.py"""

import os
import pathlib
import tempfile

tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp / "host")
os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
(tmp / "host").mkdir()
from locki.config import LockiConfig, load_config  # noqa: E402
from locki.paths import USER_CONFIG  # noqa: E402

repo = tmp / "repo"
repo.mkdir()
(repo / "locki.toml").write_text('forward_env = ["AWS_SECRET_ACCESS_KEY"]\n')
assert load_config(repo).forward_env == [], "repo config must not set forward_env"

USER_CONFIG.parent.mkdir(parents=True)
USER_CONFIG.write_text('forward_env = ["GITCODE_TOKEN"]\n')
assert load_config(repo).forward_env == ["GITCODE_TOKEN"], "user value must survive, repo value dropped"

for bad in ["GITCODE_TOKEN", ['X"; rm -rf ~; "'], ["A B"], [1], [""]]:
    try:
        LockiConfig.from_dict({"forward_env": bad})
    except SystemExit:
        continue
    raise AssertionError(f"accepted invalid forward_env: {bad!r}")

print("test_forward_env: OK")
