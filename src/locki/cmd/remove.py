import logging
import shutil
import sys

import click

from locki.paths import WORKTREES_META
from locki.utils import resolve_sandbox, run_command, run_in_vm

logger = logging.getLogger(__name__)


@click.command()
@click.option("-m", "--match", default=None, help="Sandbox branch (substring match).")
@click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("-a", "--all", "all_repos", is_flag=True, default=False, help="Consider sandboxes from all repos.")
@click.option("--force", "-f", is_flag=True, default=False, help="Skip safety checks.")
@click.option("--delete-branch", is_flag=True, default=False, help="Also delete the git branch.")
def remove_cmd(match, interactive, all_repos, force, delete_branch):
    """Remove a sandbox's worktree, includes, and container."""
    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        all_repos=all_repos,
        allow_create=False,
    )
    if sandbox is None:
        click.echo("No sandbox selected.")
        return

    wt_path = sandbox.wt_path
    if not wt_path.exists():
        logger.info("Worktree %s no longer on disk; cleaning up metadata.", wt_path)

    if (
        wt_path.exists()
        and not force
        and run_command(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            "Checking for uncommitted changes",
            check=False,
        ).stdout.strip()
    ):
        logger.error(
            "Worktree for %s in %s has uncommitted changes. Commit or stash them, or use --force.",
            sandbox.branch,
            wt_path,
        )
        sys.exit(1)

    # Remove includes first — each is a worktree in a different repo.
    for inc in sandbox.includes:
        inc_wt = sandbox.include_wt_path(inc.name)
        run_command(
            ["git", "-C", str(inc.repo), "worktree", "remove", "--force", str(inc_wt)],
            f"Removing include worktree {inc.name}",
            check=False,
        )
        run_command(
            ["git", "-C", str(inc.repo), "worktree", "prune"],
            f"Pruning {inc.repo.name}",
            check=False,
        )
        if delete_branch:
            run_command(
                ["git", "-C", str(inc.repo), "branch", "-D", inc.branch],
                f"Deleting include branch {inc.branch}",
                check=False,
            )

    run_in_vm(
        ["incus", "delete", "--force", sandbox.wt_id],
        "Deleting container",
        check=False,
    )

    shutil.rmtree(wt_path, ignore_errors=True)
    shutil.rmtree(WORKTREES_META / sandbox.wt_id, ignore_errors=True)
    run_command(
        ["git", "-C", str(sandbox.repo), "worktree", "prune"],
        "Pruning primary worktree",
        check=False,
    )

    if delete_branch:
        run_command(
            ["git", "-C", str(sandbox.repo), "branch", "-D", sandbox.branch],
            f"Deleting branch {sandbox.branch}",
            check=False,
        )
