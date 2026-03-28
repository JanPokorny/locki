"""MCP server exposing limited host operations to locki sandboxed agents.

Implements the MCP streamable HTTP transport (2024-11-05) using stdlib only.
Runs on the host so it has access to git credentials, SSH agent, and gh CLI.

Agents authenticate implicitly: every tool call includes the worktree_path,
which contains a random hex segment unknown to other containers.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCKI_HOME = pathlib.Path.home() / ".locki"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
MCP_PORT = 7890

_TOOLS = [
    {
        "name": "git_commit",
        "description": "Stage all changes and create a git commit in a locki worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worktree_path": {
                    "type": "string",
                    "description": "Absolute path to the locki worktree (your current working directory).",
                },
                "message": {"type": "string", "description": "Commit message."},
            },
            "required": ["worktree_path", "message"],
        },
    },
    {
        "name": "git_push",
        "description": "Push the current branch to a remote.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worktree_path": {
                    "type": "string",
                    "description": "Absolute path to the locki worktree.",
                },
                "remote": {"type": "string", "description": "Remote name (default: origin)."},
            },
            "required": ["worktree_path"],
        },
    },
    {
        "name": "gh_pr_create",
        "description": "Create a GitHub pull request for the current branch of a locki worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worktree_path": {
                    "type": "string",
                    "description": "Absolute path to the locki worktree.",
                },
                "title": {"type": "string", "description": "PR title."},
                "body": {"type": "string", "description": "PR description."},
                "base": {"type": "string", "description": "Base branch (default: main)."},
            },
            "required": ["worktree_path", "title"],
        },
    },
]


def _validate_worktree(worktree_path: str) -> pathlib.Path:
    wt = pathlib.Path(worktree_path)
    if not wt.is_relative_to(WORKTREES_HOME):
        raise ValueError(f"Not a locki worktree: {worktree_path!r}")
    if not wt.is_dir():
        raise ValueError(f"Worktree does not exist: {worktree_path!r}")
    return wt


def _call_tool(name: str, arguments: dict) -> str:
    if name == "git_commit":
        wt = _validate_worktree(arguments["worktree_path"])
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", arguments["message"]],
            capture_output=True,
            text=True,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(output)
        return output

    if name == "git_push":
        wt = _validate_worktree(arguments["worktree_path"])
        remote = arguments.get("remote", "origin")
        branch = subprocess.check_output(
            ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
        ).strip()
        result = subprocess.run(
            ["git", "-C", str(wt), "push", "-u", remote, branch],
            capture_output=True,
            text=True,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(output)
        return output

    if name == "gh_pr_create":
        wt = _validate_worktree(arguments["worktree_path"])
        cmd = [
            "gh", "pr", "create",
            "--title", arguments["title"],
            "--body", arguments.get("body", ""),
            "--base", arguments.get("base", "main"),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(wt))
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(output)
        return output

    raise ValueError(f"Unknown tool: {name!r}")


def _handle_jsonrpc(request: dict) -> dict | None:
    """Process one JSON-RPC request and return the response, or None for notifications."""
    method = request.get("method")
    req_id = request.get("id")

    # Notifications have no id and expect no response
    if req_id is None:
        return None

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
            "result": {"tools": _TOOLS},
        }

    if method == "tools/call":
        tool_name = request["params"]["name"]
        arguments = request["params"].get("arguments", {})
        try:
            result = _call_tool(tool_name, arguments)
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
