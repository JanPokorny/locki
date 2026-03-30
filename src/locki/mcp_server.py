"""MCP server exposing allowed host commands to locki sandboxed agents.

Single tool: run_host_command(worktree_path, exe, args)

Commands are validated against a small, explicitly enumerated allowlist.
Each rule specifies exact positional args and the permitted set of long flags.
Short flags and any unlisted flag are rejected outright.

Agents authenticate implicitly: worktree_path contains a random hex segment
unknown to other containers.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCKI_HOME = pathlib.Path.home() / ".locki"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
MCP_PORT = 7890


# ── allowlist DSL ─────────────────────────────────────────────────────────────

# Validators: called with str | None (None = flag absent, "" = boolean --flag).
_flag = lambda s: s == ""                       # --flag  (no value)
_opt  = lambda v: lambda s: s is None or v(s)  # optional, validated when present


def _cmd(*spec_args, **spec_flags):
    """Build a predicate for one allowed command pattern.

    spec_args  — positional matchers: str exact, set membership, callable predicate.
    spec_flags — flag matchers (hyphens → underscores); each is called with the
                 flag's value (str) or None if absent and must return True to pass.
    """
    def match(positionals: list[str], flags: dict[str, str]) -> bool:
        if len(positionals) != len(spec_args):
            return False
        for val, spec in zip(positionals, spec_args):
            if isinstance(spec, str)  and val != spec:    return False
            if isinstance(spec, set)  and val not in spec: return False
            if callable(spec)         and not spec(val):   return False
        for key in flags:
            if key not in spec_flags:
                return False  # unlisted flag — reject
        for key, spec in spec_flags.items():
            if not _val_ok(flags.get(key), spec):
                return False
        return True
    return match


def _val_ok(val: str | None, spec) -> bool:
    if callable(spec):        return bool(spec(val))
    if isinstance(spec, set): return val in spec
    if isinstance(spec, str): return val == spec
    return True


# Allowlist — add a _cmd(...) line to permit a new operation.
_RULES: dict[str, list] = {
    "git": [
        _cmd("status"),
        _cmd("diff"),
        _cmd("diff",   staged=_flag),
        _cmd("add",    all=_flag),
        _cmd("commit", message=bool),
        _cmd("push"),
        _cmd("fetch"),
        _cmd("log"),
        _cmd("log",    oneline=_flag),
        _cmd("show"),
    ],
    "gh": [
        _cmd("pr",    "create", title=bool, body=_opt(bool), base=_opt(bool)),
        _cmd("pr",    "view"),
        _cmd("pr",    "view",   str.isdigit),
        _cmd("pr",    "list"),
        _cmd("pr",    "diff"),
        _cmd("pr",    "status"),
        _cmd("run",   "list"),
        _cmd("run",   "view"),
        _cmd("run",   "view",   str.isdigit),
        _cmd("issue", "create", title=bool, body=_opt(bool)),
        _cmd("issue", "view"),
        _cmd("issue", "view",   str.isdigit),
        _cmd("issue", "list"),
    ],
}


# ── parsing ───────────────────────────────────────────────────────────────────

def _parse(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split args into positionals and long flags.

    --flag=value  →  flags["flag"] = "value"
    --flag        →  flags["flag"] = ""       (boolean flag, empty-string sentinel)
    -x            →  ValueError  (short flags not accepted)

    Hyphens in flag names are normalised to underscores.
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    for arg in args:
        if arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            flags[key.replace("-", "_")] = value
        elif arg.startswith("-"):
            raise ValueError(
                f"Short flags are not allowed: {arg!r}. "
                "Use the long form (--flag or --flag=value)."
            )
        else:
            positionals.append(arg)
    return positionals, flags


# ── worktree / execution ──────────────────────────────────────────────────────

def _validate_worktree(worktree_path: str) -> pathlib.Path:
    wt = pathlib.Path(worktree_path)
    if not wt.is_relative_to(WORKTREES_HOME):
        raise ValueError(f"Not a locki worktree: {worktree_path!r}")
    if not wt.is_dir():
        raise ValueError(f"Worktree does not exist: {worktree_path!r}")
    return wt


def _run_host_command(worktree_path: str, exe: str, args: list[str]) -> str:
    wt = _validate_worktree(worktree_path)

    if exe not in _RULES:
        raise ValueError(f"Executable {exe!r} is not allowed; use 'git' or 'gh'")

    positionals, flags = _parse(args)
    if not any(rule(positionals, flags) for rule in _RULES[exe]):
        raise ValueError(f"Command not allowed: {exe} {' '.join(args)!r}")

    result = subprocess.run([exe, *args], capture_output=True, text=True, cwd=str(wt))
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output)
    return output


# ── MCP protocol ──────────────────────────────────────────────────────────────

_TOOL = {
    "name": "run_host_command",
    "description": (
        "Run an allowed git or gh command on the host, where credentials and SSH keys live. "
        "Executes with cwd=worktree_path. "
        "Only long flags are accepted (--flag or --flag=value); short flags (-x) are not."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "worktree_path": {
                "type": "string",
                "description": "Absolute path to your locki worktree (your current working directory).",
            },
            "exe":  {"type": "string", "enum": ["git", "gh"]},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Arguments passed to the executable."},
        },
        "required": ["worktree_path", "exe", "args"],
    },
}


def _handle_jsonrpc(request: dict) -> dict | None:
    method = request.get("method")
    req_id = request.get("id")
    if req_id is None:
        return None  # notification

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "locki", "version": "1.0.0"},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [_TOOL]}}

    if method == "tools/call":
        if request["params"]["name"] != "run_host_command":
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": "Unknown tool"}}
        a = request["params"].get("arguments", {})
        try:
            text = _run_host_command(a["worktree_path"], a["exe"], a["args"])
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


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
                self.send_response(202); self.end_headers(); return
            data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main():
    LOCKI_HOME.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", MCP_PORT), _MCPHandler).serve_forever()


if __name__ == "__main__":
    main()
