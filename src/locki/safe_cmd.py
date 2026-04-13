import os
import pathlib
import shlex
import subprocess
import sys

import click

_required = bool               # --flag=<non-empty value>
_flag = {None, ""}        # optional boolean flag (--flag or absent, no value)

_diff_flags = {"staged": _flag, "name_only": _flag, "stat": _flag, "name_status": _flag}
_log_flags = {"oneline": _flag, "format": ..., "max_count": ...}

RULES = [
    ("git", "status"),
    ("git", "diff", _diff_flags),
    ("git", "diff", str, _diff_flags),
    ("git", "diff", str, str, _diff_flags),
    ("git", "add", {"all": _flag}),
    ("git", "add", str, ...),
    ("git", "commit", {"message": _required, "signoff": _flag}),
    ("git", "push"),
    ("git", "fetch"),
    ("git", "log", _log_flags),
    ("git", "log", str, _log_flags),
    ("git", "show"),
    ("git", "show", str),
    ("git", "restore", str, ..., {"staged": _flag, "source": ...}),
    ("git", "switch", str),
    ("git", "switch", {"create": _required}),
    ("git", "stash", "push", {"message": ...}),
    ("git", "stash", "push"),
    ("git", "stash", "list"),
    ("git", "stash", "pop"),
    ("git", "stash", "pop", str),
    ("git", "stash", "apply"),
    ("git", "stash", "apply", str),
    ("git", "stash", "drop"),
    ("git", "stash", "drop", str),
    ("gh", "pr", "create", {"title": _required, "body": ..., "base": ..., "draft": _flag, "fill": _flag, "reviewer": ..., "label": ..., "assignee": ..., "head": ...}),
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
    ("locki", "port-forward", lambda s: s.startswith(":") and s[1:].isdigit(), ...),
]


def matches(rule: tuple, positionals: list[str], flags: dict[str, str]) -> bool:
    """Test whether positionals+flags match a rule tuple.

    A rule is a tuple of positional specs, optionally ending with a dict of
    flag specs.  Positional specs: str (exact), set (membership), callable
    (predicate).  Ellipsis (...) after a positional spec means zero or more
    additional positionals are allowed.  --help is always permitted.
    """
    if rule and isinstance(rule[-1], dict):
        spec_args, spec_flags = rule[:-1], {"help": _flag, **rule[-1]}
    else:
        spec_args, spec_flags = rule, {"help": _flag}

    has_varargs = ... in spec_args
    if has_varargs:
        fixed_specs = spec_args[:spec_args.index(...)]
        if len(positionals) < len(fixed_specs):
            return False
        for val, spec in zip(positionals[:len(fixed_specs)], fixed_specs):
            if isinstance(spec, str) and val != spec:   return False
            if isinstance(spec, set) and val not in spec: return False
            if callable(spec) and not spec(val):          return False
    else:
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


def _get_branch(wt_id: str) -> str | None:
    """Read the initial branch name from worktree metadata."""
    import locki
    branch_file = locki.WORKTREES_META / wt_id / "branch"
    if branch_file.exists():
        return branch_file.read_text().strip()
    return None


def _validate_switch(wt_id: str, positionals: list[str], flags: dict[str, str]):
    """Validate that the target branch is allowed for git switch."""
    initial = _get_branch(wt_id)
    if not initial:
        print("No initial branch recorded for this worktree. git switch is not available.", file=sys.stderr)
        raise SystemExit(1)

    target = flags.get("create")
    if target:
        pass
    elif len(positionals) >= 3:
        target = positionals[2]
    else:
        print("No branch specified.", file=sys.stderr)
        raise SystemExit(1)

    if target != initial and not target.startswith(initial + "/"):
        print(f"Branch '{target}' is not allowed. Must be '{initial}' or '{initial}/<suffix>'.", file=sys.stderr)
        raise SystemExit(1)


def _handle_stash_push(wt_id: str, flags: dict[str, str]):
    """Auto-prefix stash message with [branch] for branch-scoped stashing."""
    branch = _get_branch(wt_id) or "unknown"
    prefix = f"[{branch}]"
    msg = flags.get("message", "")
    full_msg = f"{prefix} {msg}" if msg else prefix
    os.execvp("git", ["git", "stash", "push", f"--message={full_msg}"])


def _handle_stash_list(wt_id: str):
    """Show only stashes belonging to the current branch."""
    branch = _get_branch(wt_id) or "unknown"
    prefix = f"[{branch}]"
    result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if prefix in line:
            print(line)
    raise SystemExit(0 if result.returncode == 0 else result.returncode)


def _handle_stash_pop_apply_drop(wt_id: str, positionals: list[str]):
    """For pop/apply/drop: scope operations to the current branch's stashes."""
    action = positionals[2]  # "pop", "apply", or "drop"
    ref = positionals[3] if len(positionals) >= 4 else None
    branch = _get_branch(wt_id) or "unknown"
    prefix = f"[{branch}]"

    result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
    stash_lines = result.stdout.splitlines()

    if ref:
        for line in stash_lines:
            if line.startswith(ref + ":") and prefix in line:
                os.execvp("git", ["git", "stash", action, ref])
        print(f"Stash {ref} does not belong to branch '{branch}'.", file=sys.stderr)
        raise SystemExit(1)
    else:
        for line in stash_lines:
            if prefix in line:
                found_ref = line.split(":", 1)[0]
                os.execvp("git", ["git", "stash", action, found_ref])
        print(f"No stashes found for branch '{branch}'.", file=sys.stderr)
        raise SystemExit(1)


@click.command(hidden=True)
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

    # Command-specific handlers
    if exe == "git" and positionals[:1] == ["switch"]:
        _validate_switch(wt_id, [exe, *positionals], flags)
        os.execvp(exe, [exe, *argv[1:]])
    elif exe == "git" and positionals[:2] == ["stash", "push"]:
        _handle_stash_push(wt_id, flags)
    elif exe == "git" and positionals[:2] == ["stash", "list"]:
        _handle_stash_list(wt_id)
    elif exe == "git" and positionals[:1] == ["stash"] and len(positionals) >= 2 and positionals[1] in ("pop", "apply", "drop"):
        _handle_stash_pop_apply_drop(wt_id, [exe, *positionals])
    elif exe == "locki":
        os.execvp(sys.executable, [sys.executable, "-m", "locki", *argv[1:]])
    else:
        os.execvp(exe, [exe, *argv[1:]])
