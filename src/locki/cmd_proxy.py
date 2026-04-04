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

# Flag specs: used as values in the flags dict (last element of a rule tuple).
_required = bool          # --flag=<non-empty value>
_flag = {None, ""}        # optional boolean flag (--flag or absent, no value)


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


def _matches(rule: tuple, positionals: list[str], flags: dict[str, str]) -> bool:
    """Test whether positionals+flags match a rule tuple.

    A rule is a tuple of positional specs, optionally ending with a dict of
    flag specs.  Positional specs: str (exact), set (membership), callable
    (predicate).  --help is always permitted.
    """
    if rule and isinstance(rule[-1], dict):
        spec_args, spec_flags = rule[:-1], {"help": _flag, **rule[-1]}
    else:
        spec_args, spec_flags = rule, {"help": _flag}
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
            return False
    return all(_val_ok(flags.get(key), spec) for key, spec in spec_flags.items())


# Allowlist — each tuple is (positional_specs..., {flag_specs}) or just (positional_specs...).
_RULES = [
    ("git", "status"),
    ("git", "diff", {"staged": _flag}),
    ("git", "diff", str, {"staged": _flag}),
    ("git", "diff", str, str, {"staged": _flag}),
    ("git", "add", {"all": _flag}),
    ("git", "commit", {"message": _required}),
    ("git", "push"),
    ("git", "fetch"),
    ("git", "log", {"oneline": _flag}),
    ("git", "log", str, {"oneline": _flag}),
    ("git", "show"),
    ("git", "show", str),
    ("git", "restore", str, {"staged": _flag, "source": ...}),
    ("gh", "pr", "create", {"title": _required, "body": ..., "base": ...}),
    ("gh", "pr", "view"),
    ("gh", "pr", "view", str.isdigit),
    ("gh", "pr", "list"),
    ("gh", "pr", "diff"),
    ("gh", "pr", "status"),
    ("gh", "run", "list"),
    ("gh", "run", "view"),
    ("gh", "run", "view", str.isdigit),
    ("gh", "issue", "create", {"title": _required, "body": ...}),
    ("gh", "issue", "view"),
    ("gh", "issue", "view", str.isdigit),
    ("gh", "issue", "list"),
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
    if not any(_matches(rule, [exe, *positionals], flags) for rule in _RULES):
        raise ValueError(f"Command not allowed: {' '.join(argv)!r}")
    return exe, argv[1:]
