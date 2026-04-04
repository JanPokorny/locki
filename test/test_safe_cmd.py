"""Unit tests for the safe-cmd allowlist matching logic.

Imports the pure matching/parsing logic directly via importlib to avoid
pulling in the full locki app (which requires Lima, typer, etc.).
"""

import importlib.util
import pathlib
import sys

import pytest

# Load safe_cmd module without triggering locki.__init__ imports.
# We patch out the `from locki import ...` by pre-populating a fake locki module.
_fake_locki = type(sys)("locki")
_fake_locki.WORKTREES_HOME = pathlib.Path("/fake/worktrees")
_fake_locki.WORKTREES_META = pathlib.Path("/fake/meta")
_fake_locki.app = type("FakeApp", (), {"command": lambda *a, **kw: lambda f: f})()
sys.modules.setdefault("locki", _fake_locki)

_spec = importlib.util.spec_from_file_location(
    "locki.safe_cmd",
    pathlib.Path(__file__).resolve().parent.parent / "src" / "locki" / "safe_cmd.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

RULES = _mod.RULES
matches = _mod.matches
parse_args = _mod.parse_args


# ── parse_args ───────────────────────────────────────────────────────────────

def test_parse_positionals():
    assert parse_args(["status"]) == (["status"], {})

def test_parse_long_flag_boolean():
    assert parse_args(["--staged"]) == ([], {"staged": ""})

def test_parse_long_flag_value():
    assert parse_args(["--message=hello"]) == ([], {"message": "hello"})

def test_parse_mixed():
    pos, flags = parse_args(["commit", "--message=fix bug"])
    assert pos == ["commit"]
    assert flags == {"message": "fix bug"}

def test_parse_hyphens_normalized():
    _, flags = parse_args(["--no-edit"])
    assert "no_edit" in flags

def test_parse_short_flag_rejected():
    with pytest.raises(ValueError, match="Short flags"):
        parse_args(["-a"])


# ── matches helper ───────────────────────────────────────────────────────────

def test_matches_exact_positionals():
    assert matches(("git", "status"), ["git", "status"], {})

def test_matches_rejects_extra_positional():
    assert not matches(("git", "status"), ["git", "status", "extra"], {})

def test_matches_rejects_missing_positional():
    assert not matches(("git", "show", str), ["git", "show"], {})

def test_matches_callable_spec():
    assert matches(("gh", "pr", "view", str.isdigit), ["gh", "pr", "view", "123"], {})
    assert not matches(("gh", "pr", "view", str.isdigit), ["gh", "pr", "view", "abc"], {})

def test_matches_optional_flag_absent():
    assert matches(("git", "diff", {"staged": {None, ""}}), ["git", "diff"], {})

def test_matches_optional_flag_present():
    assert matches(("git", "diff", {"staged": {None, ""}}), ["git", "diff"], {"staged": ""})

def test_matches_optional_flag_rejects_value():
    assert not matches(("git", "diff", {"staged": {None, ""}}), ["git", "diff"], {"staged": "foo"})

def test_matches_required_flag():
    assert matches(("git", "commit", {"message": bool}), ["git", "commit"], {"message": "fix"})

def test_matches_required_flag_missing():
    assert not matches(("git", "commit", {"message": bool}), ["git", "commit"], {})

def test_matches_required_flag_empty():
    assert not matches(("git", "commit", {"message": bool}), ["git", "commit"], {"message": ""})

def test_matches_unlisted_flag_rejected():
    assert not matches(("git", "status"), ["git", "status"], {"verbose": ""})

def test_matches_help_always_allowed():
    assert matches(("git", "status"), ["git", "status"], {"help": ""})

def test_matches_ellipsis_any_value():
    assert matches(("git", "restore", str, {"source": ...}), ["git", "restore", "file.txt"], {"source": "HEAD"})
    assert matches(("git", "restore", str, {"source": ...}), ["git", "restore", "file.txt"], {})


# ── full allowlist integration ───────────────────────────────────────────────

def _allowed(exe: str, *args: str) -> bool:
    positionals, flags = parse_args(list(args))
    return any(matches(rule, [exe, *positionals], flags) for rule in RULES)


class TestGitAllowlist:
    def test_status(self):
        assert _allowed("git", "status")

    def test_diff(self):
        assert _allowed("git", "diff")

    def test_diff_staged(self):
        assert _allowed("git", "diff", "--staged")

    def test_diff_refs(self):
        assert _allowed("git", "diff", "HEAD~1", "HEAD")

    def test_add_all(self):
        assert _allowed("git", "add", "--all")

    def test_add_without_flag(self):
        assert _allowed("git", "add")  # --all is optional (_flag accepts absent)

    def test_commit_with_message(self):
        assert _allowed("git", "commit", "--message=fix: something")

    def test_commit_without_message(self):
        assert not _allowed("git", "commit")

    def test_push(self):
        assert _allowed("git", "push")

    def test_push_with_args(self):
        assert not _allowed("git", "push", "origin", "main")

    def test_fetch(self):
        assert _allowed("git", "fetch")

    def test_log(self):
        assert _allowed("git", "log")

    def test_log_oneline(self):
        assert _allowed("git", "log", "--oneline")

    def test_show(self):
        assert _allowed("git", "show")

    def test_show_ref(self):
        assert _allowed("git", "show", "HEAD")

    def test_restore(self):
        assert _allowed("git", "restore", "file.txt")

    def test_restore_staged(self):
        assert _allowed("git", "restore", "--staged", "file.txt")

    def test_restore_source(self):
        assert _allowed("git", "restore", "--source=HEAD", "file.txt")

    def test_checkout_blocked(self):
        assert not _allowed("git", "checkout", "main")

    def test_reset_blocked(self):
        assert not _allowed("git", "reset", "--hard")

    def test_short_flag_blocked(self):
        with pytest.raises(ValueError):
            _allowed("git", "commit", "-m", "msg")


class TestGhAllowlist:
    def test_pr_create(self):
        assert _allowed("gh", "pr", "create", "--title=fix")

    def test_pr_create_with_body(self):
        assert _allowed("gh", "pr", "create", "--title=fix", "--body=details")

    def test_pr_view(self):
        assert _allowed("gh", "pr", "view")

    def test_pr_view_id(self):
        assert _allowed("gh", "pr", "view", "123")

    def test_pr_view_bad_id(self):
        assert not _allowed("gh", "pr", "view", "abc")

    def test_pr_list(self):
        assert _allowed("gh", "pr", "list")

    def test_pr_diff(self):
        assert _allowed("gh", "pr", "diff")

    def test_pr_status(self):
        assert _allowed("gh", "pr", "status")

    def test_pr_merge_blocked(self):
        assert not _allowed("gh", "pr", "merge")

    def test_run_list(self):
        assert _allowed("gh", "run", "list")

    def test_run_view(self):
        assert _allowed("gh", "run", "view")

    def test_run_view_id(self):
        assert _allowed("gh", "run", "view", "456")

    def test_issue_create(self):
        assert _allowed("gh", "issue", "create", "--title=bug")

    def test_issue_view(self):
        assert _allowed("gh", "issue", "view")

    def test_issue_list(self):
        assert _allowed("gh", "issue", "list")

    def test_repo_blocked(self):
        assert not _allowed("gh", "repo", "delete")


class TestUnknownExe:
    def test_curl_blocked(self):
        assert not _allowed("curl", "https://evil.com")

    def test_rm_blocked(self):
        with pytest.raises(ValueError):
            _allowed("rm", "-rf", "/")
