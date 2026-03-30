"""MCP server exposing allowed host commands to locki sandboxed agents.

Single tool: run_host_command(worktree_path, exe, args)

Commands are validated against a small, explicitly enumerated allowlist.
Each rule specifies exact positional args and the permitted set of long flags.
Short flags and any unlisted flag are rejected outright.

Agents authenticate implicitly: worktree_path contains a random base36 token
unknown to other containers.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCKI_HOME = pathlib.Path.home() / ".locki"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
WORKTREES_META = LOCKI_HOME / "worktrees-meta"
MCP_PORT = 7890


# ── allowlist DSL ─────────────────────────────────────────────────────────────

# Validators: called with str | None (None = flag absent, "" = boolean --flag).
_required = bool  # --flag=<non-empty value>


def _cmd(*spec_args, **spec_flags):
    """Build a predicate for one allowed command pattern.

    spec_args  — positional matchers: str exact, set membership, callable predicate.
    spec_flags — flag matchers (hyphens → underscores); each is called with the
                 flag's value (str) or None if absent and must return True to pass.
    --help is always permitted.
    """
    spec_flags = {"help": ..., **spec_flags}
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
    if spec is ...:           return True
    if callable(spec):        return bool(spec(val))
    if isinstance(spec, set): return val in spec
    if isinstance(spec, str): return val == spec
    return True


# Allowlist — add a _cmd(...) line to permit a new operation.
_RULES: dict[str, list] = {
    "git": [
        _cmd("status"),
        _cmd("diff",            staged=...),
        _cmd("diff",  str,      staged=...),
        _cmd("diff",  str, str, staged=...),
        _cmd("add",    all=...),
        _cmd("commit", message=_required),
        _cmd("push"),
        _cmd("fetch"),
        _cmd("log",             oneline=...),
        _cmd("log",   str,      oneline=...),
        _cmd("show"),
        _cmd("show",  str),
        _cmd("restore", str,    staged=..., source=...),
    ],
    "gh": [
        _cmd("pr",    "create", title=_required, body=..., base=...),
        _cmd("pr",    "view"),
        _cmd("pr",    "view",   str.isdigit),
        _cmd("pr",    "list"),
        _cmd("pr",    "diff"),
        _cmd("pr",    "status"),
        _cmd("run",   "list"),
        _cmd("run",   "view"),
        _cmd("run",   "view",   str.isdigit),
        _cmd("issue", "create", title=_required, body=...),
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
    wt = pathlib.Path(worktree_path).resolve()
    if not wt.is_relative_to(WORKTREES_HOME.resolve()):
        raise ValueError(f"Not a locki worktree: {worktree_path!r}")
    if not wt.is_dir():
        raise ValueError(f"Worktree does not exist: {worktree_path!r}")

    # Verify .git file hasn't been tampered with (prevents git hook injection).
    wt_id = wt.relative_to(WORKTREES_HOME).parts[0]
    meta_git = WORKTREES_META / wt_id / ".git"
    if not meta_git.exists():
        raise ValueError(f"No worktree metadata found for {wt_id!r}; re-create the worktree.")
    dot_git = wt / ".git"
    if not dot_git.is_file():
        raise ValueError(f"Worktree .git is not a file — possible tampering detected.")
    if dot_git.read_text().strip() != meta_git.read_text().strip():
        raise ValueError(f"Worktree .git content mismatch — possible tampering detected.")

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
        "Only long flags are accepted (--flag or --flag=value); short flags (-x) are not.\n\n"
        "git and gh are not available directly inside the sandbox — use this tool instead.\n\n"
        "Allowed commands:\n"
        "  git status\n"
        "  git diff [--staged] [<ref> [<ref>]]\n"
        "  git add --all\n"
        "  git commit --message=<msg>\n"
        "  git push\n"
        "  git fetch\n"
        "  git log [--oneline] [<ref>]\n"
        "  git show [<ref>]\n"
        "  git restore [--staged] [--source=<ref>] <file>\n\n"
        "  gh pr create --title=<title> [--body=<body>] [--base=<base>]\n"
        "  gh pr view [<id>]  /  gh pr list  /  gh pr diff  /  gh pr status\n"
        "  gh run view [<id>]  /  gh run list\n"
        "  gh issue create --title=<title> [--body=<body>]\n"
        "  gh issue view [<id>]  /  gh issue list"
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
