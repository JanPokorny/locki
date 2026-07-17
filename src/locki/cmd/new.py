import json

import click

from locki.runes import INFO, SUCCESS
from locki.utils import (
    SandboxInfo,
    add_worktree,
    cwd_git_repo,
    fail,
    json_option,
    new_sandbox,
    pretty_path,
    run_command,
)


def create_sandbox_worktree(sandbox: SandboxInfo, from_ref: str | None = None) -> None:
    run_command(
        ["git", "-C", str(sandbox.repo), "worktree", "prune"],
        "Pruning stale git worktrees",
        print_success=False,
    )
    add_worktree(sandbox.repo, sandbox.wt_id, branch=sandbox.branch, from_ref=from_ref)
    locki_dir = sandbox.wt_path / ".locki"
    locki_dir.mkdir(parents=True, exist_ok=True)
    (locki_dir / ".gitignore").write_text("*\n")
    (locki_dir / "tmp").mkdir(exist_ok=True)


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
    cwd_repo = cwd_git_repo()
    if cwd_repo is None:
        fail("Cannot create a sandbox outside a git repo.")
    sandbox = new_sandbox(cwd_repo, branch_stem)
    create_sandbox_worktree(sandbox, from_ref)
    if as_json:
        click.echo(json.dumps(dict(sandbox)))
        return
    click.echo(f"{SUCCESS} Created sandbox {click.style(sandbox.wt_id, fg='green')}.", err=True)
    click.echo(f"{INFO}    branch: {click.style(sandbox.branch, fg='green')}", err=True)
    click.echo(f"{INFO}   on disk: {click.style(pretty_path(sandbox.wt_path), fg='green')}", err=True)
    click.echo(
        f"{INFO}  enter it: {click.style(f'locki x -m {sandbox.wt_id}', fg='green')}"
        f" (or {click.style(f'locki ai -m {sandbox.wt_id}', fg='green')})",
        err=True,
    )
