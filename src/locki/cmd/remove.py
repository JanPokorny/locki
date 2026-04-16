import logging
import shutil
import sys

import click

from locki.paths import WORKTREES, WORKTREES_META
from locki.utils import (
    current_worktree,
    find_worktree_for_branch,
    git_root,
    list_locki_worktree_branches,
    match_sandbox_branch,
    run_command,
    run_in_vm,
)

logger = logging.getLogger(__name__)


def _select_worktree_branch() -> str | None:
    """Show interactive fuzzy selector for Locki worktree branches."""
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    wt_branches = list_locki_worktree_branches()
    if not wt_branches:
        return None

    choices = [Choice(value=b, name=b) for b in sorted(wt_branches)]
    return inquirer.fuzzy(
        message="Select a branch to remove:",
        choices=choices,
    ).execute()


@click.command()
@click.option("-b", "--branch", default=None, help="Sandbox branch (substring match).")
@click.option("--force", "-f", is_flag=True, help="Skip safety checks.")
@click.option("--delete-branch", is_flag=True, help="Also delete the git branch.")
def remove_cmd(branch, force, delete_branch):
    """Remove a branch's worktree and container."""
    if branch:
        branch = match_sandbox_branch(branch)
        wt_path = find_worktree_for_branch(branch)
    else:
        wt_path = current_worktree()
        if wt_path is None:
            if not sys.stdin.isatty():
                logger.error("No branch specified. Use -b <branch> in non-interactive mode.")
                sys.exit(1)
            branch = _select_worktree_branch()
            if branch is None:
                click.echo("No Locki worktrees to remove.")
                return
            wt_path = find_worktree_for_branch(branch)

    if wt_path is None:
        logger.info("No locki-managed worktree found for '%s', nothing to do.", branch)
        return

    if (
        not force
        and run_command(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            "Checking for uncommitted changes",
            check=False,
        ).stdout.strip()
    ):
        logger.error(
            "Worktree for %s in %s has uncommitted changes. Commit or stash them, or use --force.",
            branch,
            wt_path,
        )
        sys.exit(1)

    if delete_branch and not branch:
        result = run_command(
            ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            "Resolving branch name",
            check=False,
        )
        branch = result.stdout.decode().strip() if result.returncode == 0 else None

    wt_id = wt_path.relative_to(WORKTREES).parts[0]

    run_in_vm(
        ["incus", "delete", "--force", wt_id],
        "Deleting container",
        check=False,
    )

    shutil.rmtree(wt_path, ignore_errors=True)
    shutil.rmtree(WORKTREES_META / wt_id, ignore_errors=True)
    run_command(
        ["git", "-C", str(git_root()), "worktree", "prune"],
        "Removing worktree",
        check=False,
    )

    if delete_branch:
        run_command(
            ["git", "-C", str(git_root()), "branch", "-D", branch],
            f"Deleting branch {branch}",
            check=False,
        )
