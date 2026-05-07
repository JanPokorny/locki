import click

from locki.utils import (
    SandboxInfo,
    cwd_git_repo,
    fail,
    new_sandbox,
    run_command,
    setup_worktree_hooks,
)


def create_sandbox_worktree(sandbox: SandboxInfo) -> None:
    run_command(
        ["git", "-C", str(sandbox.repo), "worktree", "prune"],
        "Pruning stale git worktrees",
    )
    sandbox.wt_path.mkdir(parents=True, exist_ok=True)
    run_command(
        ["git", "-C", str(sandbox.repo), "branch", sandbox.branch],
        f"Creating branch {click.style(sandbox.branch, fg='green')}",
    )
    run_command(
        ["git", "-C", str(sandbox.repo), "worktree", "add", str(sandbox.wt_path), sandbox.branch],
        f"Creating worktree for {click.style(sandbox.branch, fg='green')}",
    )
    locki_dir = sandbox.wt_path / ".locki"
    locki_dir.mkdir(parents=True, exist_ok=True)
    (locki_dir / ".gitignore").write_text("*\n")
    sandbox.meta_path.mkdir(parents=True, exist_ok=True)
    (sandbox.meta_path / ".git").write_text((sandbox.wt_path / ".git").read_text())
    (sandbox.meta_path / "repo").write_text(str(sandbox.repo))
    setup_worktree_hooks(sandbox.repo, sandbox.meta_path, sandbox.wt_path)


@click.command("new | n")
def new_cmd():
    """Create a new sandbox worktree. Alternatively, pass --new to other Locki commands as a shortcut.

    \b
    Examples:
      locki new                       # create sandbox worktree
    """
    cwd_repo = cwd_git_repo()
    if cwd_repo is None:
        fail("Cannot create a sandbox outside a git repo.")
    sandbox = new_sandbox(cwd_repo)
    create_sandbox_worktree(sandbox)
    click.echo(str(sandbox.wt_path))
