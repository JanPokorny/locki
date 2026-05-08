import logging
import shutil
import subprocess

import click

from locki.runes import INFO, SUCCESS
from locki.utils import SandboxInfo, cwd_git_repo, fail, list_sandboxes, resolve_sandbox, run_command, run_in_vm

logger = logging.getLogger(__name__)


def _remove_sandbox(sandbox: SandboxInfo, *, delete_branch: bool) -> None:
    for inc in sandbox.include:
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

    shutil.rmtree(sandbox.wt_path, ignore_errors=True)
    shutil.rmtree(sandbox.meta_path, ignore_errors=True)
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


@click.command()
@click.option("-m", "--match", default=None, help="Sandbox branch (substring match).")
@click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("--force", "-f", is_flag=True, default=False, help="Skip safety checks.")
@click.option("--delete-branch", is_flag=True, default=False, help="Also delete the git branch.")
@click.option("--merged", is_flag=True, default=False, help="Remove all clean sandboxes whose branch is merged into trunk.")
def remove_cmd(match, interactive, force, delete_branch, merged):
    """Remove a sandbox."""
    if merged:
        if match or interactive:
            fail("--merged cannot be combined with --match or --interactive.")
        cwd_repo = cwd_git_repo()
        all_sandboxes = list_sandboxes()
        if cwd_repo:
            all_sandboxes = [s for s in all_sandboxes if s.repo.resolve() == cwd_repo.resolve()]

        if not all_sandboxes:
            click.echo(f"{INFO} No sandboxes to check.", err=True)
            return

        repo = all_sandboxes[0].repo
        ref = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True,
        )
        trunk = ref.stdout.strip().removeprefix("refs/remotes/origin/") if ref.returncode == 0 else next(
            (name for name in ("main", "master") if subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", name], capture_output=True,
            ).returncode == 0),
            None,
        )
        if not trunk:
            fail("Could not determine the trunk branch.")

        targets = [
            s for s in all_sandboxes
            if s.branch in subprocess.run(
                ["git", "-C", str(s.repo), "branch", "--merged", trunk, "--list", s.branch],
                capture_output=True, text=True,
            ).stdout
            and (force or not s.wt_path.exists() or not run_command(
                ["git", "-C", str(s.wt_path), "status", "--porcelain"],
                "Checking for uncommitted changes", check=False, quiet=True,
            ).stdout.strip())
        ]

        if not targets:
            click.echo(f"{INFO} No merged clean sandboxes to remove.", err=True)
            return

        click.echo(f"{INFO} Removing {len(targets)} merged sandbox(es):", err=True)
        for s in targets:
            click.echo(f"     {s.branch}", err=True)

        for s in targets:
            _remove_sandbox(s, delete_branch=delete_branch)
            click.echo(f"{SUCCESS} Removed {s.branch}", err=True)
        return

    sandbox = resolve_sandbox(match=match, interactive=interactive, create="deny")

    if not sandbox.wt_path.exists():
        logger.info("Worktree %s no longer on disk; cleaning up metadata.", sandbox.wt_path)

    if (
        sandbox.wt_path.exists()
        and not force
        and run_command(
            ["git", "-C", str(sandbox.wt_path), "status", "--porcelain"],
            "Checking for uncommitted changes", check=False,
        ).stdout.strip()
    ):
        fail(
            f"Worktree for {sandbox.branch} in {sandbox.wt_path} has uncommitted changes. Commit or stash them, or use --force."
        )

    _remove_sandbox(sandbox, delete_branch=delete_branch)
