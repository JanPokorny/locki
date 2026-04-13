import json
import logging
import pathlib
import shutil
import sys

import click

import locki
from locki.utils import run_command

logger = logging.getLogger(__name__)


@click.command()
@click.option("-b", "--branch", default=None, help="Branch name.")
@click.option("--force", "-f", is_flag=True, help="Skip safety checks.")
@click.option("--delete-branch", is_flag=True, help="Also delete the git branch.")
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
@click.option("-b", "--branch", default=None, help="Branch name.")
def stop_cmd(branch):
    """Stop a branch's container without removing it."""
    _, wt_path = locki.resolve_branch(branch)
    wt_id = wt_path.relative_to(locki.WORKTREES_HOME).parts[0]
    locki.run_in_vm(
        ["incus", "stop", wt_id],
        "Stopping container",
        check=False,
    )


@click.command()
@click.option("--all", "-a", "show_all", is_flag=True, help="Show worktrees from all repos.")
def status_cmd(show_all):
    """Show VM status and managed worktrees."""
    # VM status
    vm_status = "not created"
    try:
        result = run_command(
            [locki.limactl(), "list", "--json"],
            "Checking VM",
            env={"LIMA_HOME": str(locki.LIMA_HOME)},
            cwd="/",
            check=False,
            quiet=True,
        )
        for line in result.stdout.decode().splitlines():
            vm = json.loads(line)
            if vm.get("name") == "locki":
                vm_status = vm.get("status", "unknown").lower()
    except Exception:
        pass

    click.echo(f"VM: {vm_status}")

    # Container statuses (only if VM is running)
    containers: dict[str, str] = {}
    if vm_status == "running":
        try:
            result = locki.run_in_vm(
                ["incus", "list", "--format=csv", "--columns=n,s"],
                "Listing containers",
                check=False,
                quiet=True,
            )
            for line in result.stdout.decode().splitlines():
                parts = line.split(",", 1)
                if len(parts) == 2:
                    containers[parts[0].strip()] = parts[1].strip().lower()
        except Exception:
            pass

    # Current repo worktrees
    repo_root = locki.git_root()
    result = run_command(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        "Listing worktrees",
        quiet=True,
    )

    current_repo_wts: list[tuple[str, pathlib.Path, str]] = []
    current_path: pathlib.Path | None = None
    current_branch: str | None = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1])
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
        elif line == "" and current_path and current_branch:
            if current_path.is_relative_to(locki.WORKTREES_HOME):
                wt_id = current_path.relative_to(locki.WORKTREES_HOME).parts[0]
                current_repo_wts.append((current_branch, current_path, wt_id))

    current_repo_wt_paths = {str(wt[1]) for wt in current_repo_wts}

    if current_repo_wts:
        click.echo(f"\n{repo_root.name}:")
        for branch, _wt_path, wt_id in current_repo_wts:
            status = containers.get(wt_id, "no container")
            click.echo(f"  {branch:<30s} {status}")
    else:
        click.echo(f"\n{repo_root.name}: no worktrees")

    # Other worktrees
    other_wts: list[pathlib.Path] = []
    if locki.WORKTREES_HOME.exists():
        for d in sorted(locki.WORKTREES_HOME.iterdir()):
            if d.is_dir() and str(d) not in current_repo_wt_paths:
                other_wts.append(d)

    if other_wts and not show_all:
        click.echo(f"\n{len(other_wts)} more worktree(s) from other repos (use --all to see all)")
    elif other_wts and show_all:
        # Group other worktrees by repo
        by_repo: dict[str, list[tuple[str, str]]] = {}
        for d in other_wts:
            wt_id = d.name
            branch_file = locki.WORKTREES_META / wt_id / "branch"
            repo_file = locki.WORKTREES_META / wt_id / "repo"
            branch = branch_file.read_text().strip() if branch_file.exists() else wt_id
            repo_name = pathlib.Path(repo_file.read_text().strip()).name if repo_file.exists() else "unknown"
            by_repo.setdefault(repo_name, []).append((branch, wt_id))

        for repo_name, wts in sorted(by_repo.items()):
            click.echo(f"\n{repo_name}:")
            for branch, wt_id in wts:
                status = containers.get(wt_id, "no container")
                click.echo(f"  {branch:<30s} {status}")
