"""SSH forced command for locki: validates and executes allowed git/gh commands.

Invoked by sshd when a sandboxed container connects via SSH. Reads the
original command from SSH_ORIGINAL_COMMAND, validates it against the allowlist
and the cwd against managed worktrees, then execs the real binary.

SSH_ORIGINAL_COMMAND format (POSIX shell-quoted): <cwd> <exe> [args...]
"""

from __future__ import annotations

import os
import pathlib
import shlex
import sys

LOCKI_HOME = pathlib.Path.home() / ".locki"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
WORKTREES_META = LOCKI_HOME / "worktrees-meta"


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
            if isinstance(spec, str) and val != spec:
                return False
            if isinstance(spec, set) and val not in spec:
                return False
            if callable(spec) and not spec(val):
                return False
        for key in flags:
            if key not in spec_flags:
                return False  # unlisted flag — reject
        return all(_val_ok(flags.get(key), spec) for key, spec in spec_flags.items())

    return match


def _val_ok(val: str | None, spec) -> bool:
    if spec is ...:
        return True
    if callable(spec):
        return bool(spec(val))
    if isinstance(spec, set):
        return val in spec
    if isinstance(spec, str):
        return val == spec
    return True


# Allowlist — add a _cmd(...) line to permit a new operation.
_RULES: list = [
    _cmd("git", "status"),
    _cmd("git", "diff", staged=...),
    _cmd("git", "diff", str, staged=...),
    _cmd("git", "diff", str, str, staged=...),
    _cmd("git", "add", all=...),
    _cmd("git", "commit", message=_required),
    _cmd("git", "push"),
    _cmd("git", "fetch"),
    _cmd("git", "log", oneline=...),
    _cmd("git", "log", str, oneline=...),
    _cmd("git", "show"),
    _cmd("git", "show", str),
    _cmd("git", "restore", str, staged=..., source=...),
    _cmd("gh", "pr", "create", title=_required, body=..., base=...),
    _cmd("gh", "pr", "view"),
    _cmd("gh", "pr", "view", str.isdigit),
    _cmd("gh", "pr", "list"),
    _cmd("gh", "pr", "diff"),
    _cmd("gh", "pr", "status"),
    _cmd("gh", "run", "list"),
    _cmd("gh", "run", "view"),
    _cmd("gh", "run", "view", str.isdigit),
    _cmd("gh", "issue", "create", title=_required, body=...),
    _cmd("gh", "issue", "view"),
    _cmd("gh", "issue", "view", str.isdigit),
    _cmd("gh", "issue", "list"),
]


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


# ── validation ────────────────────────────────────────────────────────────────


def _validate_worktree(cwd: str) -> pathlib.Path:
    wt = pathlib.Path(cwd).resolve()
    if not wt.is_relative_to(WORKTREES_HOME.resolve()):
        raise ValueError(f"Not a locki worktree: {cwd!r}")
    wt_root = WORKTREES_HOME / wt.relative_to(WORKTREES_HOME).parts[0]
    if not wt_root.is_dir():
        raise ValueError(f"Worktree does not exist: {cwd!r}")

    # Verify .git file hasn't been tampered with (prevents git hook injection).
    wt_id = wt_root.relative_to(WORKTREES_HOME).parts[0]
    meta_git = WORKTREES_META / wt_id / ".git"
    if not meta_git.exists():
        raise ValueError(f"No worktree metadata found for {wt_id!r}; re-create the worktree.")
    dot_git = wt_root / ".git"
    if not dot_git.is_file():
        raise ValueError("Worktree .git is not a file — possible tampering detected.")
    if dot_git.read_text().strip() != meta_git.read_text().strip():
        raise ValueError("Worktree .git content mismatch — possible tampering detected.")

    return wt


def _validate_command(argv: list[str]) -> tuple[str, list[str]]:
    if not argv:
        raise ValueError("Empty command")
    exe = pathlib.Path(argv[0]).name  # handle full paths like /opt/locki/bin/git
    positionals, flags = _parse(argv[1:])
    if not any(rule([exe, *positionals], flags) for rule in _RULES):
        raise ValueError(f"Command not allowed: {' '.join(argv)!r}")
    return exe, argv[1:]


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not cmd:
        print("No command specified.", file=sys.stderr)
        sys.exit(1)

    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        print(f"Failed to parse command: {e}", file=sys.stderr)
        sys.exit(1)

    if len(parts) < 2:
        print("Usage: <cwd> <exe> [args...]", file=sys.stderr)
        sys.exit(1)

    cwd_str, *argv = parts

    try:
        cwd = _validate_worktree(cwd_str)
        exe, args = _validate_command(argv)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    os.chdir(str(cwd))
    os.execvp(exe, [exe, *args])


if __name__ == "__main__":
    main()
