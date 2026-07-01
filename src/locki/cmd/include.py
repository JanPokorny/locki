"""`locki include` — add another repo's worktree into an existing sandbox.

The included worktree lives at `<sandbox>/.locki/include/<repo>-locki-<sandbox-id>/`
and is a full git worktree of the other repo, with its own branch
`untitled#locki-<sandbox-id>` tracked in that repo (reused if it already exists).
Bridged command rules (git, gh, ...) apply identically inside included worktrees;
ownership is scoped by the parent sandbox's id.
"""

from __future__ import annotations

import json
import pathlib

import click

from locki.paths import WORKTREES
from locki.runes import INFO, SPINNER, SUCCESS
from locki.utils import (
    add_worktree,
    cwd_git_repo,
    fail,
    json_option,
    resolve_sandbox,
    run_command,
    sandbox_options,
)


def _validate_repo(path: pathlib.Path) -> pathlib.Path:
    result = run_command(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        "Resolving repo",
        check=False,
        quiet=True,
    )
    if result.returncode != 0:
        fail(f"Not a git repository: {path}")
    return pathlib.Path(result.stdout.decode().strip()).resolve()


@click.command("include")
@sandbox_options()
@click.option("--repo", "repo_path", default=None, type=click.Path(exists=True), help="Local path to repo to include.")
@click.option(
    "--this",
    "this_flag",
    is_flag=True,
    default=False,
    help="Include cwd's repo into a sandbox from another repo (flips match scope).",
)
@json_option
def include_cmd(match, interactive, repo_path, this_flag, as_json):
    """Include another repo's worktree in an existing Locki sandbox.

    \b
    Examples:
      locki include --repo ../other-repo      # include ../other-repo into current sandbox
      locki include -m feat --repo ../other   # include into a specific sandbox
      locki include --this                    # include cwd's repo into some OTHER sandbox
      locki include --this -m feat            # include cwd's repo into sandbox matching 'feat'
    """
    if this_flag and repo_path:
        fail("--this and --repo are mutually exclusive.")

    # Resolve repo B (the one being added).
    if this_flag:
        cwd_repo = cwd_git_repo()
        if cwd_repo is None:
            fail("--this requires being inside a git repo.")
        repo_b = cwd_repo
    elif repo_path:
        repo_b = _validate_repo(pathlib.Path(repo_path))
    else:
        # Default: add cwd's repo — only sensible when cwd is in a repo different from the
        # implicit-target sandbox's repo.  Reject to force the user to be explicit.
        fail("Specify --repo <path> or use --this.")

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        create="deny",
        filter_out_current_repo=this_flag,
    )

    if sandbox.repo.resolve() == repo_b.resolve():
        fail("Cannot include a sandbox's own primary repo.")

    name = f"{repo_b.name}-locki-{sandbox.wt_id}"
    existing = {inc.name for inc in sandbox.include}
    if name in existing or sandbox.include_wt_path(name).exists() or sandbox.include_meta_path(name).exists():
        fail(f"Include {name!r} already exists in sandbox {sandbox.wt_id}. Remove it first.")

    click.echo(
        f"{SPINNER} Including "
        f"{click.style(repo_b.name, fg='green')} in sandbox {click.style(sandbox.wt_id, fg='green')}.",
        err=True,
    )
    include_wt = add_worktree(repo_b, sandbox.wt_id, parent_name=sandbox.repo.name)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "id": sandbox.wt_id,
                    "name": name,
                    "repo": str(repo_b),
                    "path": str(include_wt),
                }
            )
        )
        return
    click.echo(
        f"{SUCCESS} Included at {click.style(str(include_wt.relative_to(WORKTREES)), fg='cyan')}.",
        err=True,
    )
    click.echo(
        f"{INFO} Enter the sandbox with {click.style(f'locki x -m {sandbox.wt_id}', fg='green')}.",
        err=True,
    )
