import os
import pathlib
import shlex
import sys

_required = bool               # --flag=<non-empty value>
_flag = {None, ""}        # optional boolean flag (--flag or absent, no value)

RULES = [
    ("git", "status"),
    ("git", "diff", {"staged": _flag}),
    ("git", "diff", str, {"staged": _flag}),
    ("git", "diff", str, str, {"staged": _flag}),
    ("git", "add", {"all": _flag}),
    ("git", "commit", {"message": _required, "signoff": _flag}),
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
    ("gh", "issue", "view"),
    ("gh", "issue", "view", str.isdigit),
    ("gh", "issue", "list"),
]


def matches(rule: tuple, positionals: list[str], flags: dict[str, str]) -> bool:
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
        if isinstance(spec, str) and val != spec:   return False
        if isinstance(spec, set) and val not in spec: return False
        if callable(spec) and not spec(val):          return False
    if any(key not in spec_flags for key in flags):
        return False
    for key, spec in spec_flags.items():
        val = flags.get(key)
        if spec is ...:                              continue
        if callable(spec) and not spec(val):         return False
        elif isinstance(spec, set) and val not in spec: return False
        elif isinstance(spec, str) and val != spec:  return False
    return True


def parse_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split args into positionals and long flags.

    Raises ValueError for short flags.
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    for arg in args:
        if arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            flags[key.replace("-", "_")] = value
        elif arg.startswith("-"):
            raise ValueError(f"Short flags not allowed: {arg!r}")
        else:
            positionals.append(arg)
    return positionals, flags


def safe_cmd():
    """SSH forced command: validate and execute an allowed git/gh command."""
    import locki

    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not cmd:
        print("No command specified.", file=sys.stderr)
        raise SystemExit(1)

    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        print(f"Failed to parse command: {e}", file=sys.stderr)
        raise SystemExit(1)

    if len(parts) < 2:
        print("Usage: <cwd> <exe> [args...]", file=sys.stderr)
        raise SystemExit(1)

    cwd_str, *argv = parts

    # Validate worktree
    cwd = pathlib.Path(cwd_str).resolve()
    if not cwd.is_relative_to(locki.WORKTREES_HOME.resolve()):
        print(f"Not a locki worktree: {cwd_str!r}", file=sys.stderr)
        raise SystemExit(1)
    wt_root = locki.WORKTREES_HOME / cwd.relative_to(locki.WORKTREES_HOME).parts[0]
    wt_id = wt_root.name
    meta_git = locki.WORKTREES_META / wt_id / ".git"
    dot_git = wt_root / ".git"
    if not wt_root.is_dir() or not meta_git.exists() or not dot_git.is_file():
        print(f"Invalid worktree: {cwd_str!r}", file=sys.stderr)
        raise SystemExit(1)
    if dot_git.read_text().strip() != meta_git.read_text().strip():
        print("Worktree .git mismatch — possible tampering.", file=sys.stderr)
        raise SystemExit(1)

    # Validate command against allowlist
    if not argv:
        print("Empty command.", file=sys.stderr)
        raise SystemExit(1)
    exe = pathlib.Path(argv[0]).name
    try:
        positionals, flags = parse_args(argv[1:])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
    if not any(matches(rule, [exe, *positionals], flags) for rule in RULES):
        print(f"Command not allowed: {' '.join(argv)!r}", file=sys.stderr)
        raise SystemExit(1)

    os.chdir(str(cwd))
    os.execvp(exe, [exe, *argv[1:]])
