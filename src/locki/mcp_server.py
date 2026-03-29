"""MCP server exposing allowed host commands to locki sandboxed agents.

Single tool: run_host_command(worktree_path, exe, args)
- exe is restricted to "git" or "gh"
- args are checked against an allowlist of subcommand prefixes
- path-redirecting flags are blocked so agents cannot escape their worktree
- the command runs with cwd=worktree_path on the host (where credentials live)

Agents authenticate implicitly: worktree_path contains a random hex segment
that other containers don't know, so they cannot forge calls on each other's
worktrees.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCKI_HOME = pathlib.Path.home() / ".locki"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
MCP_PORT = 7890

# Allowed first arg(s) per executable. A prefix of length N means the first
# N elements of args must match. Anything beyond the prefix is passed through.
_GIT_ALLOWED: frozenset[tuple[str, ...]] = frozenset([
    # read-only
    ("branch",),
    ("diff",),
    ("fetch",),
    ("log",),
    ("show",),
    ("stash", "list"),
    ("stash", "show"),
    ("status",),
    # own-worktree writes
    ("add",),
    ("checkout",),      # e.g. git checkout other-branch -- path/to/file
    ("commit",),
    ("push",),
    ("restore",),
    ("stash", "apply"),
    ("stash", "drop"),
    ("stash", "pop"),
    ("stash", "push"),
    ("tag",),
])

_GH_ALLOWED: frozenset[tuple[str, ...]] = frozenset([
    ("issue", "comment"),
    ("issue", "create"),
    ("issue", "list"),
    ("issue", "view"),
    ("pr", "comment"),
    ("pr", "create"),
    ("pr", "diff"),
    ("pr", "list"),
    ("pr", "review"),
    ("pr", "status"),
    ("pr", "view"),
    ("repo", "view"),
    ("run", "list"),
    ("run", "view"),
    ("workflow", "list"),
    ("workflow", "view"),
])

# Flags that could redirect git to a different directory or repo — always blocked.
_GIT_BLOCKED_FLAGS: frozenset[str] = frozenset(["-C", "--git-dir", "--work-tree"])
_GH_BLOCKED_FLAGS: frozenset[str] = frozenset(["--repo", "-R"])


def _validate_worktree(worktree_path: str) -> pathlib.Path:
    wt = pathlib.Path(worktree_path)
    if not wt.is_relative_to(WORKTREES_HOME):
        raise ValueError(f"Not a locki worktree: {worktree_path!r}")
    if not wt.is_dir():
        raise ValueError(f"Worktree does not exist: {worktree_path!r}")
    return wt


def _check_allowed(exe: str, args: list[str]) -> None:
    """Raise ValueError if (exe, args) is not permitted."""
    if not args:
        raise ValueError("args must not be empty")

    if exe == "git":
        for arg in args:
            if arg in _GIT_BLOCKED_FLAGS:
                raise ValueError(f"Flag {arg!r} is not allowed (would redirect to a different directory)")
        if args[0] == "push" and ("--force" in args or "-f" in args):
            raise ValueError("git push --force is not allowed")
        allowed = _GIT_ALLOWED

    elif exe == "gh":
        for arg in args:
            if arg in _GH_BLOCKED_FLAGS:
                raise ValueError(f"Flag {arg!r} is not allowed (would target a different repo)")
        allowed = _GH_ALLOWED

    else:
        raise ValueError(f"Executable {exe!r} is not allowed; use 'git' or 'gh'")

    for prefix in allowed:
        if tuple(args[: len(prefix)]) == prefix:
            return

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
