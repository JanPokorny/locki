import logging
import pathlib
import shutil
import sys

import click

from locki.config import WORKTREES_HOME, WORKTREES_META
from locki.utils import (
    current_worktree,
    find_worktree_for_branch,
    git_root,
    resolve_branch,
    run_command,
    run_in_vm,
)

logger = logging.getLogger(__name__)


@click.command()
@click.option("-b", "--branch", default=None, help="Branch name.")
@click.option("--force", "-f", is_flag=True, help="Skip safety checks.")
@click.option("--delete-branch", is_flag=True, help="Also delete the git branch.")
def remove_cmd(branch, force, delete_branch):
    """Remove a branch's worktree and container."""
    if branch:
        wt_path = find_worktree_for_branch(branch)
    else:
        wt_path = current_worktree()
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

    wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]

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


@click.command()
@click.option("-b", "--branch", default=None, help="Branch name.")
def stop_cmd(branch):
    """Stop a branch's container without removing it."""
    _, wt_path = resolve_branch(branch)
    wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]
    run_in_vm(
        ["incus", "stop", wt_id],
        "Stopping container",
        check=False,
    )


@click.command()
def list_cmd():
    """List Locki worktrees in the current repo."""
    repo_root = git_root()
    result = run_command(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        "Listing worktrees",
        quiet=True,
    )

    home = pathlib.Path.home()
    rows: list[tuple[str, str, str]] = []
    current_path: pathlib.Path | None = None
    current_branch: str | None = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1])
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
        elif line == "" and current_path and current_branch:
            if current_path.is_relative_to(WORKTREES_HOME):
                title_file = current_path / ".locki" / "title"
                title = title_file.read_text().strip() if title_file.exists() else ""
                if title == "<no title generated yet>":
                    title = ""
                path_str = str(current_path)
                if current_path.is_relative_to(home):
                    path_str = "~/" + str(current_path.relative_to(home))
                rows.append((title, current_branch, path_str))

    if not rows:
        click.echo("No Locki worktrees in this repo.")
        return

    # Compute column widths
    headers = ("TITLE", "BRANCH", "PATH")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    fmt = f"{{:<{widths[0]}}}  {{:<{widths[1]}}}  {{}}"
    click.echo(fmt.format(*headers))
    for row in rows:
        click.echo(fmt.format(*row))
