"""`locki include` — add another repo's worktree into an existing sandbox.

The included worktree lives at `<sandbox>/.locki/include/<name>/` and is a
full git worktree of the other repo, with its own branch `untitled#locki-<sandbox-id>`
tracked in that repo.  Git / gh self-service proxy rules apply identically inside
included worktrees; ownership is scoped by the parent sandbox's id.
"""

from __future__ import annotations

import importlib.resources
import logging
import pathlib
import sys

import click

from locki.cmd.exec import GIT_HOOKS
from locki.paths import WORKTREES
from locki.runes import ERROR, INFO, SPINNER, SUCCESS
from locki.utils import (
    SandboxInfo,
    cwd_git_repo,
    resolve_sandbox,
    run_command,
)

logger = logging.getLogger(__name__)


def _validate_repo(path: pathlib.Path) -> pathlib.Path:
    result = run_command(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        "Resolving repo",
        check=False,
        quiet=True,
    )
    if result.returncode != 0:
        click.echo(
            f"{ERROR} Not a git repository: {path}",
            err=True,
        )
        sys.exit(1)
    return pathlib.Path(result.stdout.decode().strip()).resolve()


def _setup_include(sandbox: SandboxInfo, repo_b: pathlib.Path, name: str) -> None:
    """Create branch, worktree, meta, hooks, config for an include."""
    include_wt = sandbox.include_wt_path(name)
    include_meta = sandbox.include_meta_path(name)

    if include_wt.exists() or include_meta.exists():
        click.echo(
            f"{ERROR} Include {name!r} already exists in sandbox {sandbox.wt_id}.",
            err=True,
        )
        sys.exit(1)

    branch = f"untitled#locki-{sandbox.wt_id}"

    # In repo B: create branch from current HEAD, add worktree.
    # If the branch already exists (e.g. another include of the same sandbox in the
    # past, cleaned up but branch survived) we reuse it.
    check = run_command(
        ["git", "-C", str(repo_b), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        "Checking for existing include branch",
        check=False,
        quiet=True,
    )
    if check.returncode != 0:
        run_command(
            ["git", "-C", str(repo_b), "branch", branch],
            f"Creating branch {click.style(branch, fg='green')} in {repo_b.name}",
        )

    include_wt.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        ["git", "-C", str(repo_b), "worktree", "add", str(include_wt), branch],
        f"Creating worktree for {click.style(name, fg='green')}",
    )

    include_meta.mkdir(parents=True, exist_ok=True)
    (include_meta / ".git").write_text((include_wt / ".git").read_text())
    (include_meta / "repo").write_text(str(repo_b))

    run_command(
        ["git", "-C", str(repo_b), "config", "extensions.worktreeConfig", "true"],
        "Enabling per-worktree git config",
    )

    hooks_dir = include_meta / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_script = (importlib.resources.files("locki") / "data" / "locki-hook.sh").read_bytes()
    for hook in GIT_HOOKS:
        hook_path = hooks_dir / hook
        hook_path.write_bytes(hook_script)
        hook_path.chmod(0o755)

    run_command(
        ["git", "-C", str(include_wt), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
        "Configuring per-worktree hooks",
    )
    run_command(
        ["git", "-C", str(include_wt), "config", "--worktree", "push.autoSetupRemote", "true"],
        "Configuring auto push for new branches",
    )


@click.command("include")
@click.option("-m", "--match", default=None, help="Target sandbox branch (substring match).")
@click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive sandbox picker.")
@click.option("-a", "--all", "all_repos", is_flag=True, default=False, help="Show sandboxes from all repos.")
@click.option("--repo", "repo_path", default=None, type=click.Path(exists=True), help="Local path to repo to include.")
@click.option(
    "--this",
    "this_flag",
    is_flag=True,
    default=False,
    help="Include cwd's repo into a sandbox from another repo (flips match scope).",
)
def include_cmd(match, interactive, all_repos, repo_path, this_flag):
    """Include another repo's worktree in an existing Locki sandbox.

    \b
    Examples:
      locki include --repo ../other-repo      # include ../other-repo into current sandbox
      locki include -m feat --repo ../other   # include into a specific sandbox
      locki include --this                    # include cwd's repo into some OTHER sandbox
      locki include --this -m feat            # include cwd's repo into sandbox matching 'feat'
    """
    if this_flag and repo_path:
        click.echo(
            f"{ERROR} --this and --repo are mutually exclusive.",
            err=True,
        )
        sys.exit(1)

    # Resolve repo B (the one being added).
    if this_flag:
        cwd_repo = cwd_git_repo()
        if cwd_repo is None:
            click.echo(
                f"{ERROR} --this requires being inside a git repo.",
                err=True,
            )
            sys.exit(1)
        repo_b = cwd_repo
    elif repo_path:
        repo_b = _validate_repo(pathlib.Path(repo_path))
    else:
        # Default: add cwd's repo — only sensible when cwd is in a repo different from the
        # implicit-target sandbox's repo.  Reject to force the user to be explicit.
        click.echo(
            f"{ERROR} Specify --repo <path> or use --this.",
            err=True,
        )
        sys.exit(1)

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        all_repos=all_repos,
        allow_create=False,
        filter_out_current_repo=this_flag,
    )
    if sandbox is None:
        click.echo(
            f"{ERROR} locki include needs an existing sandbox.",
            err=True,
        )
        sys.exit(1)

    if sandbox.repo.resolve() == repo_b.resolve():
        click.echo(
            f"{ERROR} Cannot include a sandbox's own primary repo.",
            err=True,
        )
        sys.exit(1)

    name = repo_b.name
    existing = {inc.name for inc in sandbox.include}
    if name in existing:
        click.echo(
            f"{ERROR} Include {name!r} already exists in sandbox {sandbox.wt_id}. "
            f"Remove it first.",
            err=True,
        )
        sys.exit(1)

    click.echo(
        f"{SPINNER} Including "
        f"{click.style(repo_b.name, fg='green')} in sandbox {click.style(sandbox.wt_id, fg='green')}.",
        err=True,
    )
    _setup_include(sandbox, repo_b, name)
    click.echo(
        f"{SUCCESS} Included at "
        f"{click.style(str(sandbox.include_wt_path(name).relative_to(WORKTREES)), fg='cyan')}.",
        err=True,
    )
    click.echo(
        f"{INFO} Enter the sandbox with "
        f"{click.style(f'locki x -m {sandbox.wt_id}', fg='green')}.",
        err=True,
    )
