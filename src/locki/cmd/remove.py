import json
import logging
import os
import pathlib
import sys

import click

from locki.runes import INFO, SUCCESS, WARNING
from locki.services.container import containers
from locki.services.worktree import WorktreeInfo, worktrees
from locki.utils import fail, json_option, run_command, sandbox_options

logger = logging.getLogger(__name__)


def _porcelain(root: pathlib.Path) -> list[str]:
    result = run_command(
        ["git", "-C", str(root), "status", "--porcelain"], "Checking for changes", check=False, quiet=True
    )
    return [line for line in result.stdout.decode().splitlines() if line.strip()]


def _locki_tmp_files(root: pathlib.Path) -> list[str]:
    """Regular files under the worktree's .locki/tmp/, as worktree-relative paths.

    These are invisible to the porcelain check (.locki/.gitignore is `*`), so
    they need their own scan."""
    return sorted(
        str(f.relative_to(root))
        for dirpath, _dirnames, filenames in os.walk(root / ".locki" / "tmp", followlinks=False)
        for name in filenames
        if not (f := pathlib.Path(dirpath) / name).is_symlink() and f.is_file()
    )


def _unsaved_work(worktree: WorktreeInfo) -> dict[str, list[str]]:
    """Uncommitted changes `locki rm` would destroy, per tree: the worktree
    itself and each include (which is force-removed, so git won't stop it)."""
    lost: dict[str, list[str]] = {}
    if lines := _porcelain(worktree.path):
        lost["uncommitted changes"] = lines
    for inc in worktree.include:
        inc_path = worktree.include_path(inc.name)
        if inc_path.exists() and (lines := _porcelain(inc_path)):
            lost[f"include {inc.name}"] = lines
    return lost


def remove_sandbox(worktree: WorktreeInfo, *, branches: bool, force: bool, as_json: bool) -> None:
    """Remove a sandbox, refusing (or asking) first when removal would lose data."""
    if worktree.path.exists() and not force:
        unsaved = _unsaved_work(worktree)
        tmp = _locki_tmp_files(worktree.path)
        if as_json or not sys.stdin.isatty():
            if unsaved:
                fail(
                    f"Sandbox {worktree.branch} in {worktree.path} has unsaved work"
                    f" ({', '.join(unsaved)}). Commit or stash it, or use --force."
                )
            if tmp:
                click.echo(f"{WARNING} Losing .locki/tmp artifacts: {', '.join(tmp)}", err=True)
        elif unsaved or tmp:
            listing = dict(unsaved)
            if tmp:
                listing[".locki/tmp artifacts"] = tmp
            click.echo(f"{WARNING} Removing this sandbox would lose:", err=True)
            for label, lines in listing.items():
                click.echo(f"{WARNING}   {label}:", err=True)
                for line in lines:
                    click.echo(f"{WARNING}     {line}", err=True)
            click.confirm("Delete anyway (lose the files)?", abort=True, err=True)
    containers.remove(worktree.wt_id)
    worktrees.remove(worktree, branches=branches)


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

        trunk = worktrees.trunk(all_sandboxes[0].repo)
        if not trunk:
            fail("Could not determine the trunk branch.")

        targets = [
            s
            for s in all_sandboxes
            if worktrees.is_merged(s.repo, trunk, s.branch) and (force or not s.path.exists() or not _unsaved_work(s))
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
            if s.path.exists() and (tmp := _locki_tmp_files(s.path)):
                click.echo(f"{WARNING} {s.branch}: losing .locki/tmp artifacts: {', '.join(tmp)}", err=True)

        containers.remove(*(s.wt_id for s in targets))
        for s in targets:
            worktrees.remove(s, branches=branches)
            click.echo(f"{SUCCESS} Removed {s.branch}", err=True)
        if as_json:
            click.echo(json.dumps([s.as_dict() for s in targets]))
        return

    worktree = worktrees.resolve(match=match, interactive=interactive, create="deny")

    if not worktree.path.exists():
        logger.info("Worktree %s no longer on disk; cleaning up metadata.", worktree.path)

    remove_sandbox(worktree, branches=branches, force=force, as_json=as_json)
    if as_json:
        click.echo(json.dumps([worktree.as_dict()]))
