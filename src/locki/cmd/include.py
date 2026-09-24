"""`locki include` — add another worktree into an existing sandbox.

The included worktree lives at `<sandbox>/.locki/include/<repo>[-<n>]-locki-<sandbox-id>/`
and is a full git worktree of the included repo, with its own branch
`untitled[-<n>]#locki-<sandbox-id>` tracked in that repo (reused if it already exists).
Any repo can be included any number of times, the sandbox's own repo too — the n-th
copy gets the `-<n>` suffix.  Bridged command rules (git, gh, ...) apply identically
inside included worktrees; ownership is scoped by the parent sandbox's id.
"""

from __future__ import annotations

import json
import pathlib

import click

from locki.paths import WORKTREES
from locki.runes import INFO, SPINNER, SUCCESS
from locki.services.worktree import worktrees
from locki.utils import fail, json_option, sandbox_options


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
    """Include another worktree of a repo in an existing Locki sandbox.

    \b
    Examples:
      locki include --repo ../other-repo      # include ../other-repo into current sandbox
      locki include -m feat --repo ../other   # include into a specific sandbox
      locki include --this                    # include cwd's repo into some OTHER sandbox
      locki include --this -m feat            # include cwd's repo into sandbox matching 'feat'

    Inside a sandbox folder, the current sandbox is picked implicitly, so `--this`
    there adds another worktree of the repo you're in to its own sandbox.
    """
    if this_flag == bool(repo_path):
        fail("Specify either --repo <path> or --this.")

    repo = worktrees.repo_of(pathlib.Path(repo_path or "."))
    if repo is None:
        fail(f"Not a git repository: {repo_path or pathlib.Path.cwd()}")

    worktree = worktrees.resolve(match=match, interactive=interactive, create="deny", other_repos=this_flag)

    name, branch = worktrees.next_include(worktree, repo)
    click.echo(
        f"{SPINNER} Including "
        f"{click.style(repo.name, fg='green')} in sandbox {click.style(worktree.wt_id, fg='green')}.",
        err=True,
    )
    include_wt = worktrees.add(repo, worktree.wt_id, parent_name=worktree.repo.name, branch=branch, dir_name=name)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "id": worktree.wt_id,
                    "name": name,
                    "repo": str(repo),
                    "branch": branch,
                    "path": str(include_wt),
                }
            )
        )
        return
    click.echo(
        f"{SUCCESS} Included at {click.style(str(include_wt.relative_to(WORKTREES)), fg='cyan')}"
        f" on branch {click.style(branch, fg='green')}.",
        err=True,
    )
    click.echo(
        f"{INFO} Enter the sandbox with {click.style(f'locki x -m {worktree.wt_id}', fg='green')}.",
        err=True,
    )
