"""MCP server exposing allowed host commands to locki sandboxed agents.

Single tool: run_host_command(worktree_path, exe, args)
- exe is restricted to "git" or "gh"
- args are validated against an explicit allowlist: each rule specifies
  a required subcommand prefix and the exact set of flags permitted beyond it
- non-flag positional args (paths, refs, commit hashes) are always allowed
- path-redirecting flags are blocked so agents cannot escape their worktree
- the command runs with cwd=worktree_path on the host (where credentials live)

Agents authenticate implicitly: worktree_path contains a random hex segment
that other containers don't know, so they cannot forge calls on each other's
worktrees.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple

LOCKI_HOME = pathlib.Path.home() / ".locki"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
MCP_PORT = 7890


class _Rule(NamedTuple):
    """One entry in an allowlist.

    prefix      — args must start with exactly these strings.
    allowed_flags — flags (args starting with '-') permitted beyond the prefix.
                    Non-flag args (paths, refs, messages) are always allowed.
    """

    prefix: tuple[str, ...]
    allowed_flags: frozenset[str] = frozenset()


_GIT_RULES: tuple[_Rule, ...] = (
    # ── read-only ────────────────────────────────────────────────────────────
    _Rule(("branch",),      frozenset(["-a", "--all", "-r", "--remotes",
                                       "-v", "--verbose", "-l", "--list",
                                       "--sort", "--contains"])),
    _Rule(("diff",),        frozenset(["--staged", "--cached",
                                       "--stat", "--name-only", "--name-status",
                                       "-p", "--patch", "-U", "--unified",
                                       "--diff-filter"])),
    _Rule(("fetch",),       frozenset(["--all", "--prune", "-p",
                                       "--tags", "-t", "--dry-run", "-n"])),
    _Rule(("log",),         frozenset(["--oneline", "--graph", "--decorate",
                                       "--all", "--follow",
                                       "-n", "--format", "--pretty",
                                       "--since", "--until", "--after", "--before",
                                       "--author", "--grep",
                                       "--no-merges", "--merges",
                                       "--stat", "--name-only",
                                       "-p", "--patch"])),
    _Rule(("show",),        frozenset(["--stat", "--name-only", "--name-status",
                                       "--format", "--pretty",
                                       "-p", "--patch", "--no-patch"])),
    _Rule(("stash", "list"),  frozenset()),
    _Rule(("stash", "show"),  frozenset(["-p", "--patch", "--stat", "--name-only"])),
    _Rule(("status",),      frozenset(["-s", "--short", "-b", "--branch",
                                       "--porcelain",
                                       "-u", "--untracked-files"])),
    _Rule(("tag",),         frozenset(["-l", "--list", "-n",
                                       "--sort", "--contains",
                                       "--merged", "--no-merged"])),
    # ── own-worktree writes ───────────────────────────────────────────────────
    _Rule(("add",),         frozenset(["-A", "--all",
                                       "-p", "--patch",
                                       "-u", "--update",
                                       "-n", "--dry-run"])),
    _Rule(("checkout",),    frozenset(["--", "-b", "-B"])),
    _Rule(("commit",),      frozenset(["-m", "--message",
                                       "--amend", "--no-edit",
                                       "-a", "--all",
                                       "--allow-empty",
                                       "--no-verify"])),
    _Rule(("push",),        frozenset(["-u", "--set-upstream", "--tags"])),
    _Rule(("restore",),     frozenset(["--staged", "--worktree",
                                       "-s", "--source", "--"])),
    _Rule(("stash", "apply"),  frozenset(["--index"])),
    _Rule(("stash", "drop"),   frozenset()),
    _Rule(("stash", "pop"),    frozenset(["--index"])),
    _Rule(("stash", "push"),   frozenset(["-u", "--include-untracked",
                                           "-m", "--message",
                                           "-p", "--patch"])),
)

_GH_RULES: tuple[_Rule, ...] = (
    # ── issues ───────────────────────────────────────────────────────────────
    _Rule(("issue", "comment"),  frozenset(["-b", "--body", "-F", "--body-file"])),
    _Rule(("issue", "create"),   frozenset(["-t", "--title",
                                            "-b", "--body", "-F", "--body-file",
                                            "-l", "--label",
                                            "-a", "--assignee",
                                            "-m", "--milestone",
                                            "-p", "--project",
                                            "--web"])),
    _Rule(("issue", "list"),     frozenset(["-a", "--assignee", "-A", "--author",
                                            "-l", "--label",
                                            "-L", "--limit",
                                            "-m", "--milestone",
                                            "-s", "--state",
                                            "--web", "--json", "--jq"])),
    _Rule(("issue", "view"),     frozenset(["-c", "--comments",
                                            "--json", "--jq",
                                            "-w", "--web"])),
    # ── pull requests ─────────────────────────────────────────────────────────
    _Rule(("pr", "comment"),     frozenset(["-b", "--body", "-F", "--body-file",
                                            "--edit-last", "--reply-to"])),
    _Rule(("pr", "create"),      frozenset(["-t", "--title",
                                            "-b", "--body", "-F", "--body-file",
                                            "-B", "--base", "-H", "--head",
                                            "-d", "--draft",
                                            "-l", "--label",
                                            "-a", "--assignee",
                                            "-m", "--milestone",
                                            "-p", "--project",
                                            "-r", "--reviewer",
                                            "-f", "--fill", "--fill-verbose",
                                            "--web"])),
    _Rule(("pr", "diff"),        frozenset(["--patch", "--name-only", "--color"])),
    _Rule(("pr", "list"),        frozenset(["-a", "--assignee", "-A", "--author",
                                            "-B", "--base", "-d", "--draft",
                                            "-H", "--head",
                                            "-l", "--label",
                                            "-L", "--limit",
                                            "-s", "--state",
                                            "--json", "--jq",
                                            "-w", "--web"])),
    _Rule(("pr", "review"),      frozenset(["-a", "--approve",
                                            "-c", "--comment",
                                            "-r", "--request-changes",
                                            "-b", "--body", "-F", "--body-file"])),
    _Rule(("pr", "status"),      frozenset(["--json", "--jq"])),
    _Rule(("pr", "view"),        frozenset(["-c", "--comments",
                                            "--json", "--jq",
                                            "-w", "--web"])),
    # ── repo / CI ─────────────────────────────────────────────────────────────
    _Rule(("repo", "view"),      frozenset(["-b", "--branch",
                                            "--json", "--jq",
                                            "-w", "--web"])),
    _Rule(("run", "list"),       frozenset(["-b", "--branch", "-c", "--commit",
                                            "-e", "--event",
                                            "-L", "--limit",
                                            "-s", "--status",
                                            "-u", "--user",
                                            "-w", "--workflow",
                                            "--json", "--jq"])),
    _Rule(("run", "view"),       frozenset(["--exit-status",
                                            "-j", "--job",
                                            "--log", "--log-failed",
                                            "-v", "--verbose",
                                            "-w", "--web",
                                            "--json", "--jq"])),
    _Rule(("workflow", "list"),  frozenset(["-a", "--all",
                                            "-L", "--limit",
                                            "-r", "--ref",
                                            "--json", "--jq"])),
    _Rule(("workflow", "view"),  frozenset(["-r", "--ref",
                                            "-w", "--web",
                                            "--yaml",
                                            "--json", "--jq"])),
)

# Flags that could redirect git/gh to a different directory or repo — always blocked.
_GIT_BLOCKED_FLAGS: frozenset[str] = frozenset(["-C", "--git-dir", "--work-tree"])
_GH_BLOCKED_FLAGS: frozenset[str] = frozenset(["--repo", "-R"])

# Matches combined short-flag+value like -n5 or -U3; we check just the flag part.
_SHORT_FLAG_WITH_VALUE = re.compile(r"^(-[a-zA-Z])\d+$")


def _flag_name(arg: str) -> str:
    """Normalise an arg to just the flag name for allowlist lookup.

    --format=pretty  →  --format
    -n5              →  -n
    --patch          →  --patch
    """
    if "=" in arg:
        return arg.split("=", 1)[0]
    m = _SHORT_FLAG_WITH_VALUE.match(arg)
    if m:
        return m.group(1)
    return arg


def _validate_worktree(worktree_path: str) -> pathlib.Path:
    wt = pathlib.Path(worktree_path)
    if not wt.is_relative_to(WORKTREES_HOME):
        raise ValueError(f"Not a locki worktree: {worktree_path!r}")
    if not wt.is_dir():
        raise ValueError(f"Worktree does not exist: {worktree_path!r}")
    return wt


def _check_allowed(exe: str, args: list[str]) -> None:
    """Raise ValueError if (exe, args) is not on the allowlist."""
    if not args:
        raise ValueError("args must not be empty")

    if exe == "git":
        for arg in args:
            if _flag_name(arg) in _GIT_BLOCKED_FLAGS:
                raise ValueError(
                    f"Flag {arg!r} is not allowed (would redirect to a different directory)"
                )
        if args[0] == "push" and any(_flag_name(a) in ("--force", "-f") for a in args):
            raise ValueError("git push --force is not allowed")
        rules = _GIT_RULES

    elif exe == "gh":
        for arg in args:
            if _flag_name(arg) in _GH_BLOCKED_FLAGS:
                raise ValueError(
                    f"Flag {arg!r} is not allowed (would target a different repo)"
                )
        rules = _GH_RULES

    else:
        raise ValueError(f"Executable {exe!r} is not allowed; use 'git' or 'gh'")

    for rule in rules:
        n = len(rule.prefix)
        if tuple(args[:n]) != rule.prefix:
            continue
        # Prefix matched — now check remaining args.
        for arg in args[n:]:
            if not arg.startswith("-"):
                continue  # positional arg (path, ref, message …) — always ok
            flag = _flag_name(arg)
            if flag not in rule.allowed_flags:
                raise ValueError(
                    f"Flag {arg!r} is not allowed for"
                    f" '{exe} {' '.join(rule.prefix)}'"
                )
        return  # all good

    raise ValueError(
        f"'{exe} {' '.join(args[:3])}' is not in the allowed command list"
    )


_TOOL = {
    "name": "run_host_command",
    "description": (
        "Run an allowed git or gh command on the host, where credentials and SSH keys live. "
        "The command executes with cwd set to worktree_path. "
        "Use this for committing, pushing, opening PRs, reading diffs/logs, checking CI, etc."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "worktree_path": {
                "type": "string",
                "description": "Absolute path to your locki worktree (your current working directory).",
            },
            "exe": {
                "type": "string",
                "enum": ["git", "gh"],
                "description": "Executable to run.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments passed to the executable.",
            },
        },
        "required": ["worktree_path", "exe", "args"],
    },
}


def _run_host_command(worktree_path: str, exe: str, args: list[str]) -> str:
    wt = _validate_worktree(worktree_path)
    _check_allowed(exe, args)
    result = subprocess.run([exe, *args], capture_output=True, text=True, cwd=str(wt))
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output)
    return output


def _handle_jsonrpc(request: dict) -> dict | None:
    """Process one JSON-RPC request and return the response, or None for notifications."""
    method = request.get("method")
    req_id = request.get("id")

    if req_id is None:
        return None  # notification — no response expected

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "locki", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [_TOOL]},
        }

    if method == "tools/call":
        if request["params"]["name"] != "run_host_command":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "Unknown tool"},
            }
        a = request["params"].get("arguments", {})
        try:
            result = _run_host_command(a["worktree_path"], a["exe"], a["args"])
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": result}], "isError": False},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


class _MCPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if isinstance(body, list):
            responses = [r for req in body if (r := _handle_jsonrpc(req)) is not None]
            data = json.dumps(responses).encode()
        else:
            response = _handle_jsonrpc(body)
            if response is None:
                self.send_response(202)
                self.end_headers()
                return
            data = json.dumps(response).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # suppress per-request logs; errors still go to stderr


def main():
    LOCKI_HOME.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", MCP_PORT), _MCPHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
