"""The shared sandbox home (mounted as /root in every container): seeding it with
per-sandbox trust and agent settings, and reading agent state back out of it."""

import json
import pathlib
import re
from contextlib import suppress

import click

from locki.paths import PACKAGE_DATA, SANDBOX_HOME
from locki.runes import WARNING
from locki.utils import deep_merge


class HomeService:
    """Sandbox-home seeding + AI-harness state (Claude Code project dirs, transcripts)."""

    def claude_project_dir(self, wt_path: pathlib.Path) -> pathlib.Path:
        """Claude Code's per-project directory for a worktree (its cwd-munging scheme)."""
        return SANDBOX_HOME / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(wt_path))

    def prepare(self, wt_path: pathlib.Path) -> None:
        """Seed the shared sandbox home with per-sandbox trust and agent settings."""
        SANDBOX_HOME.mkdir(parents=True, exist_ok=True)
        for path, updates in [
            (SANDBOX_HOME / ".claude.json", {"projects": {str(wt_path): {"hasTrustDialogAccepted": True}}}),
            (
                SANDBOX_HOME / ".claude" / "settings.json",
                {
                    "skipDangerousModePermissionPrompt": True,
                    "permissions": {"defaultMode": "bypassPermissions"},
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [
                                    {"type": "command", "command": "sh /root/.claude/hooks/locki-branch-guard.sh"}
                                ],
                            }
                        ]
                    },
                },
            ),
            (
                SANDBOX_HOME / ".config" / "opencode" / "opencode.json",
                {
                    "$schema": "https://opencode.ai/config.json",
                    "permission": "allow",
                    "instructions": ["/etc/opencode/AGENTS.md"],
                },
            ),
        ]:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing = json.loads(path.read_text()) if path.exists() else {}
                path.write_text(json.dumps(deep_merge(existing, updates), indent=2))
            except json.JSONDecodeError:
                click.echo(f"{WARNING} Invalid JSON data found in {path}, not updating it.")

        guard = SANDBOX_HOME / ".claude" / "hooks" / "locki-branch-guard.sh"
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_bytes((PACKAGE_DATA / "claude-branch-guard.sh").read_bytes())

    def ai_title(self, wt_path: pathlib.Path) -> str:
        """Last AI-generated session title from the sandbox's Claude Code transcripts, or "".

        Claude Code appends `{"type":"ai-title","aiTitle":...}` lines to
        `~/.claude/projects/<munged-cwd>/<session>.jsonl`; the sandbox's `/root` is
        SANDBOX_HOME, so those are directly readable here. Internal format
        (observed on 2.1.212) -- fail soft on any surprise.
        """
        project = self.claude_project_dir(wt_path)
        for jsonl in sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            title = ""
            try:
                # ceiling: full scan, transcripts reach MBs; upgrade: tail-read (title recurs every ~25 lines)
                lines = jsonl.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                if '"type":"ai-title"' in line:
                    with suppress(json.JSONDecodeError):  # torn line from a live append
                        title = json.loads(line).get("aiTitle") or title
            if title:
                return title
        return ""


home = HomeService()
