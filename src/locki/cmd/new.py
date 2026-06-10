import contextlib
import json
import subprocess

import click

from locki.runes import INFO, SUCCESS
from locki.utils import (
    SandboxInfo,
    cwd_git_repo,
    fail,
    json_option,
    new_sandbox,
    pretty_path,
    run_command,
    setup_worktree_hooks,
)


def create_sandbox_worktree(sandbox: SandboxInfo) -> None:
    run_command(
        ["git", "-C", str(sandbox.repo), "worktree", "prune"],
        "Pruning stale git worktrees",
        print_success=False,
    )
    sandbox.wt_path.mkdir(parents=True, exist_ok=True)
    run_command(
        ["git", "-C", str(sandbox.repo), "branch", sandbox.branch],
        f"Creating branch {click.style(sandbox.branch, fg='green')}",
        print_success=False,
    )
    run_command(
        ["git", "-C", str(sandbox.repo), "worktree", "add", str(sandbox.wt_path), sandbox.branch],
        f"Creating worktree for {click.style(sandbox.branch, fg='green')}",
    )
    locki_dir = sandbox.wt_path / ".locki"
    locki_dir.mkdir(parents=True, exist_ok=True)
    (locki_dir / ".gitignore").write_text("*\n")
    (locki_dir / "tmp").mkdir(exist_ok=True)
    sandbox.meta_path.mkdir(parents=True, exist_ok=True)
    (sandbox.meta_path / ".git").write_text((sandbox.wt_path / ".git").read_text())
    (sandbox.meta_path / "repo").write_text(str(sandbox.repo))
    setup_worktree_hooks(sandbox.repo, sandbox.meta_path, sandbox.wt_path)
    with contextlib.suppress(FileNotFoundError):
        subprocess.run(["mise", "trust"], cwd=str(sandbox.wt_path), capture_output=True)


@click.command("new | n")
@json_option
def new_cmd(as_json: bool):
    """Create a new sandbox worktree. Alternatively, pass --new to other Locki commands as a shortcut.

    \b
    Examples:
      locki new                            # create sandbox worktree
      id=$(locki new --json | jq -r .id)   # capture the sandbox id in scripts
    """
    cwd_repo = cwd_git_repo()
    if cwd_repo is None:
        fail("Cannot create a sandbox outside a git repo.")
    sandbox = new_sandbox(cwd_repo)
    create_sandbox_worktree(sandbox)
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
