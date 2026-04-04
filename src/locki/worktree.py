import logging
import shutil
import sys
import typing

import typer

from locki import (
    WORKTREES_HOME,
    WORKTREES_META,
    app,
    run_in_vm,
    git_root,
    current_worktree,
    find_worktree_for_branch,
)
from locki.utils import run_command

logger = logging.getLogger(__name__)


@app.command("remove | rm | delete", help="Remove a branch's worktree and container.")
async def remove_cmd(
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name to remove (optional if inside a worktree)")
    ] = None,
    force: typing.Annotated[bool, typer.Option("--force", "-f", help="Skip safety checks")] = False,
    delete_branch: typing.Annotated[bool, typer.Option("--branch", "-b", help="Also delete the branch")] = False,
):
    if branch:
        wt_path = await find_worktree_for_branch(branch)
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
        and (
            await run_command(
                ["git", "-C", str(wt_path), "status", "--porcelain"],
                "Checking for uncommitted changes",
                check=False,
            )
        ).stdout.strip()
    ):
        logger.error(
            "Worktree for %s in %s has uncommitted changes. Commit or stash them, or use --force.",
            branch,
            wt_path,
        )
        sys.exit(1)

    if delete_branch and not branch:
        result = await run_command(
            ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            "Resolving branch name",
            check=False,
        )
        branch = result.stdout.decode().strip() if result.returncode == 0 else None

    wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]

    await run_in_vm(
        ["incus", "delete", "--force", wt_id],
        "Deleting container",
        check=False,
    )

    shutil.rmtree(wt_path, ignore_errors=True)
    shutil.rmtree(WORKTREES_META / wt_id, ignore_errors=True)
    await run_command(
        ["git", "-C", str(git_root()), "worktree", "prune"],
        "Removing worktree",
        check=False,
    )

    if delete_branch:
        await run_command(
            ["git", "-C", str(git_root()), "branch", "-D", branch],
            f"Deleting branch {branch}",
            check=False,
        )


@app.command("list | ls", help="List branches with Locki-managed worktrees.")
async def list_cmd():
    result = await run_command(
        ["git", "-C", str(git_root()), "worktree", "list", "--porcelain"],
        "Listing worktrees",
    )

    found = False
    current_path = None
    current_branch = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            import pathlib
            current_path = pathlib.Path(line.split(" ", 1)[1])
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
        elif line == "" and current_path and current_branch:
            if current_path.is_relative_to(WORKTREES_HOME):
                typer.echo(f"{current_branch}  {current_path}")
                found = True

    if not found:
        logger.info("No locki-managed worktrees found.")
