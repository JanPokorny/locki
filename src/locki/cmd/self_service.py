import os
import pathlib
import shlex
import subprocess
import sys

import click

from locki.paths import WORKTREES, WORKTREES_META

_required = bool  # --flag=<non-empty value>
_flag = {None, ""}  # optional boolean flag (--flag or absent, no value)
_present = {""}  # flag must be present (no value expected)

_diff_flags = {"staged": _flag, "name_only": _flag, "stat": _flag, "name_status": _flag}
_log_flags = {
    "oneline": _flag,
    "format": ...,
    "max_count": ...,
    "all": _flag,
    "graph": _flag,
    "reverse": _flag,
}
_pr_view_flags = {"comments": _flag}
_run_view_flags = {"log": _flag, "log_failed": _flag}
_commit_flags = {"message": _required, "signoff": _flag, "amend": _flag, "gpg_sign": _flag}
_push_flags = {"force_with_lease": _flag}
_fetch_flags = {"prune": _flag}
_pull_flags = {"rebase": _flag, "ff_only": _flag}
_state_flags = {"continue": _flag, "abort": _flag, "skip": _flag}  # rebase/cherry-pick/merge
_pr_edit_flags = {"title": ..., "body": ..., "add_label": ..., "add_reviewer": ..., "add_assignee": ...}


def _is_pr_comments_api_path(path: str) -> bool:
    """Match repos/{owner}/{repo}/pulls/{number}/comments."""
    parts = path.split("/")
    return (
        len(parts) == 6
        and parts[0] == "repos"
        and parts[3] == "pulls"
        and parts[4].isdigit()
        and parts[5] == "comments"
    )


RULES = [
    # ── git read-only ────────────────────────────────────────────────────────
    ("git", "status"),
    ("git", "diff", _diff_flags),
    ("git", "diff", str, _diff_flags),
    ("git", "diff", str, str, _diff_flags),
    ("git", "log", _log_flags),
    ("git", "log", str, _log_flags),
    ("git", "show", {"stat": _flag, "name_only": _flag, "name_status": _flag, "format": ...}),
    ("git", "show", str, ..., {"stat": _flag, "name_only": _flag, "name_status": _flag, "format": ...}),
    ("git", "blame", str),
    ("git", "branch", {"show_current": _flag}),
    ("git", "reflog"),
    # ── git write (sandbox branch) ───────────────────────────────────────────
    ("git", "add", {"all": _flag}),
    ("git", "add", str, ...),
    ("git", "commit", _commit_flags),
    ("git", "commit", {"amend": _present, "no_edit": _flag, "gpg_sign": _flag}),
    ("git", "commit", {"reuse_message": _required, "amend": _flag, "gpg_sign": _flag}),
    ("git", "push", _push_flags),
    ("git", "fetch", _fetch_flags),
    ("git", "pull", _pull_flags),
    ("git", "restore", str, ..., {"staged": _flag, "source": ...}),
    ("git", "checkout", {"detach": _present}),
    ("git", "checkout", str, {"detach": _present}),
    ("git", "switch", str, {"force_create": _flag}),
    ("git", "rebase", str),
    ("git", "rebase", _state_flags),
    ("git", "cherry-pick", str, {"no_commit": _flag, "gpg_sign": _flag}),
    ("git", "cherry-pick", _state_flags),
    ("git", "merge", str),
    ("git", "merge", _state_flags),
    ("git", "reset", str, {"hard": _flag}),
    ("git", "branch", str),
    ("git", "branch", str, {"move": _flag, "delete": _flag, "force": _flag}),
    ("git", "stash", "push", {"message": ...}),
    ("git", "stash", "push"),
    ("git", "stash", "list"),
    ("git", "stash", "pop"),
    ("git", "stash", "pop", str),
    ("git", "stash", "apply"),
    ("git", "stash", "apply", str),
    ("git", "stash", "drop"),
    ("git", "stash", "drop", str),
    # ── gh read-only ─────────────────────────────────────────────────────────
    ("gh", "pr", "view", _pr_view_flags),
    ("gh", "pr", "view", str.isdigit, _pr_view_flags),
    ("gh", "pr", "list"),
    ("gh", "pr", "diff"),
    ("gh", "pr", "status"),
    ("gh", "pr", "checks"),
    ("gh", "pr", "checks", str.isdigit),
    ("gh", "run", "list"),
    ("gh", "run", "view", _run_view_flags),
    ("gh", "run", "view", str.isdigit, _run_view_flags),
    ("gh", "issue", "view"),
    ("gh", "issue", "view", str.isdigit),
    ("gh", "issue", "list"),
    ("gh", "api", _is_pr_comments_api_path),
    # ── gh write (sandbox PR) ────────────────────────────────────────────────
    (
        "gh",
        "pr",
        "create",
        {
            "title": _required,
            "body": ...,
            "base": ...,
            "draft": _flag,
            "fill": _flag,
            "reviewer": ...,
            "label": ...,
            "assignee": ...,
            "head": ...,
        },
    ),
    ("gh", "pr", "edit", _pr_edit_flags),
    ("gh", "pr", "edit", str.isdigit, _pr_edit_flags),
    ("gh", "pr", "comment", str.isdigit, {"body": _required}),
    # ── locki ────────────────────────────────────────────────────────────────
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
        fixed_specs = spec_args[: spec_args.index(...)]
        if len(positionals) < len(fixed_specs):
            return False
        for val, spec in zip(positionals[: len(fixed_specs)], fixed_specs, strict=True):
            if isinstance(spec, str) and val != spec:
                return False
            if isinstance(spec, set) and val not in spec:
                return False
            if callable(spec) and not spec(val):
                return False
    else:
        if len(positionals) != len(spec_args):
            return False
        for val, spec in zip(positionals, spec_args, strict=True):
            if isinstance(spec, str) and val != spec:
                return False
            if isinstance(spec, set) and val not in spec:
                return False
            if callable(spec) and not spec(val):
                return False

    if any(key not in spec_flags for key in flags):
        return False
    for key, spec in spec_flags.items():
        val = flags.get(key)
        if spec is ...:
            continue
        if (
            (callable(spec) and not spec(val))
            or (isinstance(spec, set) and val not in spec)
            or (isinstance(spec, str) and val != spec)
        ):
            return False
    return True


def parse_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split args into positionals and long flags.

    Raises ValueError for short flags.
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    rest_positional = False
    for arg in args:
        if rest_positional:
            positionals.append(arg)
        elif arg == "--":
            rest_positional = True
        elif arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            flags[key.replace("-", "_")] = value
        elif arg.startswith("-"):
            raise ValueError(f"Short flags not allowed: {arg!r}")
        else:
            positionals.append(arg)
    return positionals, flags


def _wt_tag(wt_id: str) -> str:
    return f"#locki-{wt_id}"


def _validate_branch_suffix(wt_id: str, target: str):
    """Check that a branch name ends with #locki-<wt_id>."""
    tag = _wt_tag(wt_id)
    if not target.endswith(tag):
        print(f"Branch '{target}' is not allowed. Must end with '{tag}'.", file=sys.stderr)
        raise SystemExit(1)


def _validate_branch_arg(wt_id: str, positionals: list[str]):
    """Validate that the branch name argument ends with #locki-<wt_id>."""
    target = positionals[2] if len(positionals) >= 3 else None
    if not target:
        print("No branch specified.", file=sys.stderr)
        raise SystemExit(1)
    _validate_branch_suffix(wt_id, target)


def _validate_gh_api_repo(api_path: str):
    """Verify that the repo in a gh api path matches the current repo."""
    parts = api_path.split("/")
    requested = f"{parts[1]}/{parts[2]}"
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("Could not determine current repo.", file=sys.stderr)
        raise SystemExit(1)
    actual = result.stdout.strip()
    if requested != actual:
        print(f"API repo '{requested}' does not match current repo '{actual}'.", file=sys.stderr)
        raise SystemExit(1)


def _handle_stash_push(wt_id: str, flags: dict[str, str]):
    """Auto-prefix stash message with worktree tag for wt-scoped stashing."""
    tag = _wt_tag(wt_id)
    msg = flags.get("message", "")
    full_msg = f"[{tag}] {msg}" if msg else f"[{tag}]"
    os.execvp("git", ["git", "stash", "push", f"--message={full_msg}"])


def _handle_stash_list(wt_id: str):
    """Show only stashes belonging to the current worktree."""
    tag = _wt_tag(wt_id)
    result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if f"[{tag}]" in line:
            print(line)
    raise SystemExit(0 if result.returncode == 0 else result.returncode)


def _handle_stash_pop_apply_drop(wt_id: str, positionals: list[str]):
    """For pop/apply/drop: scope operations to the current worktree's stashes."""
    action = positionals[2]  # "pop", "apply", or "drop"
    ref = positionals[3] if len(positionals) >= 4 else None
    tag = _wt_tag(wt_id)

    result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
    stash_lines = result.stdout.splitlines()

    if ref:
        for line in stash_lines:
            if line.startswith(ref + ":") and f"[{tag}]" in line:
                os.execvp("git", ["git", "stash", action, ref])
        print(f"Stash {ref} does not belong to worktree '{wt_id}'.", file=sys.stderr)
        raise SystemExit(1)
    else:
        for line in stash_lines:
            if f"[{tag}]" in line:
                found_ref = line.split(":", 1)[0]
                os.execvp("git", ["git", "stash", action, found_ref])
        print(f"No stashes found for worktree '{wt_id}'.", file=sys.stderr)
        raise SystemExit(1)


@click.command(hidden=True)
def self_service_cmd():
    """SSH forced command: validate and execute an allowed self-service command."""
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not cmd:
        print("No command specified.", file=sys.stderr)
        raise SystemExit(1)

    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        print(f"Failed to parse command: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    if len(parts) < 2:
        print("Usage: <cwd> <exe> [args...]", file=sys.stderr)
        raise SystemExit(1)

    cwd_str, *argv = parts

    # Validate worktree
    cwd = pathlib.Path(cwd_str).resolve()
    if not cwd.is_relative_to(WORKTREES.resolve()):
        print(f"Not a locki worktree: {cwd_str!r}", file=sys.stderr)
        raise SystemExit(1)
    wt_root = WORKTREES / cwd.relative_to(WORKTREES).parts[0]
    wt_id = wt_root.name
    meta_git = WORKTREES_META / wt_id / ".git"
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
        raise SystemExit(1) from None
    if not any(matches(rule, [exe, *positionals], flags) for rule in RULES):
        print(f"Command not allowed: {' '.join(argv)!r}", file=sys.stderr)
        raise SystemExit(1)

    os.chdir(str(cwd))

    # Command-specific handlers
    if exe == "git" and (
        positionals[:1] == ["switch"] or (positionals[:1] == ["branch"] and "show_current" not in flags)
    ):
        _validate_branch_arg(wt_id, [exe, *positionals])
        os.execvp(exe, [exe, *argv[1:]])
    elif exe == "git" and positionals[:2] == ["stash", "push"]:
        _handle_stash_push(wt_id, flags)
    elif exe == "git" and positionals[:2] == ["stash", "list"]:
        _handle_stash_list(wt_id)
    elif (
        exe == "git"
        and positionals[:1] == ["stash"]
        and len(positionals) >= 2
        and positionals[1] in ("pop", "apply", "drop")
    ):
        _handle_stash_pop_apply_drop(wt_id, [exe, *positionals])
    elif exe == "gh" and positionals[:1] == ["api"]:
        _validate_gh_api_repo(positionals[1])
        os.execvp(exe, [exe, *argv[1:]])
    elif exe == "locki":
        os.execvp(sys.executable, [sys.executable, "-m", "locki", *argv[1:]])
    else:
        os.execvp(exe, [exe, *argv[1:]])
