from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import locki
from locki.config import LockiConfig


def test_provider_registry_contains_claude_and_codex():
    assert set(locki.PROVIDERS) == {"claude", "codex"}
    assert locki.PROVIDERS["claude"].host_state_dir == locki.CLAUDE_HOME
    assert locki.PROVIDERS["codex"].host_state_dir == locki.CODEX_HOME
    assert locki.PROVIDERS["codex"].guest_state_dir == PurePosixPath("/root/.codex")


def test_claude_launch_argv_is_unchanged():
    argv = locki.PROVIDERS["claude"].launch_argv_builder(Path("/tmp/worktree"), ["--print"])
    assert argv == [
        "mise",
        "exec",
        "nodejs@24",
        "npm:@anthropic-ai/claude-code@latest",
        "--",
        "claude",
        "--print",
    ]


def test_codex_launch_argv_includes_required_defaults():
    wt_path = Path("/tmp/worktree")
    argv = locki.PROVIDERS["codex"].launch_argv_builder(wt_path, ["--model", "gpt-5.4-mini"])
    assert argv[:8] == [
        "env",
        "CODEX_HOME=/root/.codex",
        "mise",
        "exec",
        "nodejs@24",
        "npm:@openai/codex@latest",
        "--",
        "codex",
    ]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert 'cli_auth_credentials_store="file"' in argv
    assert 'model_instructions_file="/etc/codex/LOCKI.md"' in argv
    assert f'projects."{wt_path}".trust_level="trusted"' in argv
    assert argv[-2:] == ["--model", "gpt-5.4-mini"]


def test_provider_container_files_include_managed_assets():
    wt_path = Path("/tmp/worktree")
    claude_files = locki.PROVIDERS["claude"].container_file_builder(wt_path)
    codex_files = locki.PROVIDERS["codex"].container_file_builder(wt_path)

    assert "/etc/claude-code/CLAUDE.md" in claude_files
    assert '"defaultMode": "bypassPermissions"' in claude_files["/etc/claude-code/managed-settings.json"]
    assert codex_files == {"/etc/codex/LOCKI.md": locki.data_text("CODEX.md")}


def test_incus_image_arch_aliases(monkeypatch):
    monkeypatch.setattr("locki.config.platform.machine", lambda: "x86_64")
    assert LockiConfig().get_incus_image() == "locki-base"

    monkeypatch.setattr("locki.config.platform.machine", lambda: "aarch64")
    assert LockiConfig().get_incus_image() == "locki-base"


@pytest.mark.anyio
async def test_shell_path_does_not_trigger_provider_specific_prep(monkeypatch):
    called = False

    async def fake_prepare_worktree_environment(branch):
        assert branch == "demo"
        return Path("/tmp/worktree"), "wt-demo"

    async def fake_prepare_provider_environment(*args, **kwargs):
        nonlocal called
        called = True

    class ShellExitError(RuntimeError):
        pass

    def fake_exec_shell_session(wt_id, wt_path, command, extra_args):
        assert wt_id == "wt-demo"
        assert wt_path == Path("/tmp/worktree")
        assert command is None
        assert extra_args == []
        raise ShellExitError

    monkeypatch.setattr(locki, "prepare_worktree_environment", fake_prepare_worktree_environment)
    monkeypatch.setattr(locki, "prepare_provider_environment", fake_prepare_provider_environment)
    monkeypatch.setattr(locki, "exec_shell_session", fake_exec_shell_session)

    with pytest.raises(ShellExitError):
        await locki.shell_cmd(SimpleNamespace(args=[]), branch="demo", command=None)

    assert not called
