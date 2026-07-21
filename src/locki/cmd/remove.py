import json
import logging
import subprocess

import click

from locki.runes import INFO, SUCCESS
from locki.services.container import containers
from locki.services.worktree import WorktreeInfo, worktrees
from locki.utils import fail, json_option, run_command, sandbox_options

logger = logging.getLogger(__name__)


def _is_merged(repo_path: str, trunk: str, branch: str) -> bool:
    git = ["git", "-C", repo_path]
    devnull = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
    if branch in subprocess.run([*git, "branch", "--merged", trunk, "--list", branch], **devnull).stdout:
        return True
    merge_base = subprocess.run([*git, "merge-base", trunk, branch], **devnull)
    if merge_base.returncode != 0:
        return False
    tree = subprocess.run([*git, "rev-parse", f"{branch}^{{tree}}"], **devnull)
    if tree.returncode != 0:
        return False
    squash_commit = subprocess.run(
        [*git, "commit-tree", tree.stdout.strip(), "-p", merge_base.stdout.strip(), "-m", "squash check"],
        **devnull,
    )
    if squash_commit.returncode != 0:
        return False
    cherry = subprocess.run([*git, "cherry", trunk, squash_commit.stdout.strip()], **devnull)
    return cherry.returncode == 0 and cherry.stdout.strip().startswith("-")


def _has_uncommitted_changes(worktree: WorktreeInfo, *, quiet: bool = False) -> bool:
    return bool(
        run_command(
            ["git", "-C", str(worktree.wt_path), "status", "--porcelain"],
            "Checking for uncommitted changes",
            check=False,
            quiet=quiet,
        ).stdout.strip()
    )


@click.command()
@sandbox_options()
@click.option(
    "--force", "-f", is_flag=True, default=False, help="Remove despite having uncommitted changes. (May lose work!)"
)
@click.option(
    "--branches", "-b", is_flag=True, default=False, help="Also delete all git branches belonging to this sandbox."
)
@click.option(
    "--merged", "-M", is_flag=True, default=False, help="Remove all clean sandboxes whose branch is merged into trunk."
)
@json_option
def remove_cmd(match, interactive, force, branches, merged, as_json):
    """Remove a sandbox. Container and worktree is deleted, branches remain unless --branches is passed."""
    if merged:
        if match or interactive:
            fail("--merged cannot be combined with --match or --interactive.")
        cwd_repo = worktrees.cwd_repo
        all_sandboxes = worktrees.list()
        if cwd_repo:
            all_sandboxes = [s for s in all_sandboxes if s.repo.resolve() == cwd_repo.resolve()]

        if not all_sandboxes:
            click.echo(f"{INFO} No sandboxes to check.", err=True)
            if as_json:
                click.echo(json.dumps([]))
            return

        repo = all_sandboxes[0].repo
        ref = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
        )
        if ref.returncode == 0:
            trunk = ref.stdout.strip().removeprefix("refs/remotes/origin/")
        else:
            trunk = next(
                (
                    name
                    for name in ("main", "master")
                    if subprocess.run(
                        ["git", "-C", str(repo), "rev-parse", "--verify", name],
                        capture_output=True,
                    ).returncode
                    == 0
                ),
                None,
            )
        if not trunk:
            fail("Could not determine the trunk branch.")

        targets = [
            s
            for s in all_sandboxes
            if _is_merged(str(s.repo), trunk, s.branch)
            and (force or not s.wt_path.exists() or not _has_uncommitted_changes(s, quiet=True))
        ]

        if not targets:
            click.echo(f"{INFO} No merged clean sandboxes to remove.", err=True)
            if as_json:
                click.echo(json.dumps([]))
            return

        click.echo(f"{INFO} Removing {len(targets)} merged sandbox(es):", err=True)
        for s in targets:
            click.echo(f"     {s.branch}", err=True)

        for s in targets:
            containers.remove(s.wt_id)
            worktrees.remove(s, branches=branches)
            click.echo(f"{SUCCESS} Removed {s.branch}", err=True)
        if as_json:
            click.echo(json.dumps([dict(s) for s in targets]))
        return

    worktree = worktrees.resolve(match=match, interactive=interactive, create="deny")

    if not worktree.wt_path.exists():
        logger.info("Worktree %s no longer on disk; cleaning up metadata.", worktree.wt_path)

    if worktree.wt_path.exists() and not force and _has_uncommitted_changes(worktree):
        fail(
            f"Worktree for {worktree.branch} in {worktree.wt_path} has uncommitted changes. Commit or stash them, or use --force."
        )

    containers.remove(worktree.wt_id)
    worktrees.remove(worktree, branches=branches)
    if as_json:
        click.echo(json.dumps([dict(worktree)]))
