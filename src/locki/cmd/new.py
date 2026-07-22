import json

import click

from locki.runes import INFO, SUCCESS
from locki.services.worktree import worktrees
from locki.utils import fail, json_option, pretty_path


@click.command("new | n")
@click.option("--from", "-f", "from_ref", default=None, help="Base the new branch on this ref instead of HEAD.")
@click.option("--branch", "-b", "branch_stem", default="untitled", help="Branch name stem (#locki-<id> is appended).")
@json_option
def new_cmd(as_json: bool, from_ref: str | None, branch_stem: str):
    """Create a new sandbox worktree. Alternatively, pass --new to other Locki commands as a shortcut.

    \b
    Examples:
      locki new                            # create sandbox worktree
      locki new -b my-feature              # branch named my-feature#locki-<id>
      locki new -f origin/main             # branch off origin/main
      id=$(locki new --json | jq -r .id)   # capture the sandbox id in scripts
    """
    cwd_repo = worktrees.cwd_repo
    if cwd_repo is None:
        fail("Cannot create a sandbox outside a git repo.")
    worktree = worktrees.new(cwd_repo, branch_stem)
    worktrees.create(worktree, from_ref)
    if as_json:
        click.echo(json.dumps(worktree.as_dict()))
        return
    click.echo(f"{SUCCESS} Created sandbox {click.style(worktree.wt_id, fg='green')}.", err=True)
    click.echo(f"{INFO}    branch: {click.style(worktree.branch, fg='green')}", err=True)
    click.echo(f"{INFO}   on disk: {click.style(pretty_path(worktree.wt_path), fg='green')}", err=True)
    click.echo(
        f"{INFO}  enter it: {click.style(f'locki x -m {worktree.wt_id}', fg='green')}"
        f" (or {click.style(f'locki ai -m {worktree.wt_id}', fg='green')})",
        err=True,
    )
