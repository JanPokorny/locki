import logging
import pathlib
import shutil
import sys

import click

import locki
from locki.utils import run_command

logger = logging.getLogger(__name__)


@click.command()
@click.argument("branch", required=False)
@click.option("--force", "-f", is_flag=True, help="Skip safety checks.")
@click.option("--branch", "-b", "delete_branch", is_flag=True, help="Also delete the branch.")
def remove_cmd(branch, force, delete_branch):
    """Remove a branch's worktree and container."""
    if branch:
        wt_path = locki.find_worktree_for_branch(branch)
    else:
        wt_path = locki.current_worktree()
        if wt_path is None:
            logger.error("No branch specified and not inside a locki worktree.")
            sys.exit(1)

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

    wt_id = wt_path.relative_to(locki.WORKTREES_HOME).parts[0]

    locki.run_in_vm(
        ["incus", "delete", "--force", wt_id],
        "Deleting container",
        check=False,
    )

    shutil.rmtree(wt_path, ignore_errors=True)
    shutil.rmtree(locki.WORKTREES_META / wt_id, ignore_errors=True)
    run_command(
        ["git", "-C", str(locki.git_root()), "worktree", "prune"],
        "Removing worktree",
        check=False,
    )

    if delete_branch:
        run_command(
            ["git", "-C", str(locki.git_root()), "branch", "-D", branch],
            f"Deleting branch {branch}",
            check=False,
        )


@click.command()
def list_cmd():
    """List branches with Locki-managed worktrees."""
    result = run_command(
        ["git", "-C", str(locki.git_root()), "worktree", "list", "--porcelain"],
        "Listing worktrees",
    )

    found = False
    current_path = None
    current_branch = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1])
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
        elif line == "" and current_path and current_branch:
            if current_path.is_relative_to(locki.WORKTREES_HOME):
                click.echo(f"{current_branch}  {current_path}")
                found = True

    if not found:
        logger.info("No locki-managed worktrees found.")
