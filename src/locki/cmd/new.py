import shutil
import subprocess
import sys

import click
from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from locki.utils import (
    SandboxInfo,
    cwd_git_repo,
    fail,
    new_sandbox,
    run_command,
    setup_worktree_hooks,
)

EDITORS = [
    ("code", "Open in VSCode"),
    ("zed", "Open in Zed"),
    ("fresh", "Open in Fresh"),
]


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
@click.option("-n", "--no-editor", is_flag=True, default=False, help="Skip editor selection.")
def new_cmd(no_editor):
    """Create a new sandbox worktree.

    \b
    Examples:
      locki new                       # create sandbox, offer to open editor
      locki new -n                    # create sandbox, skip editor prompt
    """
    cwd_repo = cwd_git_repo()
    if cwd_repo is None:
        fail("Cannot create a sandbox outside a git repo.")
    sandbox = new_sandbox(cwd_repo)
    create_sandbox_worktree(sandbox)
    click.echo(str(sandbox.wt_path))

    if no_editor or not sys.stdin.isatty():
        return

    available = [(cmd, label) for cmd, label in EDITORS if shutil.which(cmd)]
    if not available:
        return

    choices = [Choice(value=cmd, name=label) for cmd, label in available]
    choices.append(Choice(value=None, name="No thanks"))

    print()
    selected = inquirer.select(
        message="Worktree created. Open it now?",
        default=0,
        choices=choices,
    ).execute()

    if selected:
        subprocess.run([selected, "."], cwd=str(sandbox.wt_path))
