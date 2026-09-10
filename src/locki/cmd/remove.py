import json
import logging
import pathlib
import sys

import click

from locki.runes import INFO, SUCCESS, WARNING
from locki.services import transfer
from locki.services.container import containers
from locki.services.worktree import WorktreeInfo, worktrees
from locki.utils import fail, json_option, run_command, sandbox_options

logger = logging.getLogger(__name__)


def _porcelain(root: pathlib.Path) -> list[tuple[str, str]]:
    """(XY status, path) entries; -z output is NUL-delimited and never quoted."""
    result = run_command(
        ["git", "-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all"],
        "Checking for changes",
        check=False,
        quiet=True,
    )
    entries: list[tuple[str, str]] = []
    fields = iter(result.stdout.decode().split("\0"))
    for field in fields:
        if not field:
            continue
        xy = field[:2]
        if "R" in xy or "C" in xy:
            next(fields, None)  # a rename/copy record carries the original path in an extra field
        entries.append((xy, field[3:]))
    return entries


def _staged_divergent(xy: str) -> bool:
    """A staged version differing from the working tree lives only in the
    sandbox's index -- a copy of the working tree does not cover it."""
    return xy[0] not in " ?" and xy[1] != " "


def _losable_now(src: pathlib.Path, dst: pathlib.Path) -> list[str]:
    """Porcelain lines of *src* whose content is not already saved at its 1:1
    path in *dst* (a prior `locki file pull` rescues files ahead of time).
    Anything rescue can't copy -- deletions, symlinks, nested repos -- always
    counts, as does a staged version differing from the working tree."""
    saved = set(transfer.classify(src, dst, transfer.changed_files(src)).identical)
    return [f"{xy} {path}" for xy, path in _porcelain(src) if _staged_divergent(xy) or path not in saved]


def _unsaved_work(worktree: WorktreeInfo) -> dict[str, list[str]]:
    """Uncommitted changes `locki rm` would destroy, per tree: the worktree
    itself and each include (which is force-removed, so git won't stop it).
    These block non-interactive removal; .locki/tmp artifacts (scanned
    separately) never do."""
    lost: dict[str, list[str]] = {}
    if entries := _losable_now(worktree.path, worktree.repo):
        lost["uncommitted changes"] = entries
    for inc in worktree.include:
        inc_path = worktree.include_path(inc.name)
        if inc_path.exists() and (entries := _losable_now(inc_path, inc.repo)):
            lost[f"include {inc.name}"] = entries
    return lost


def _rescue_jobs(worktree: WorktreeInfo) -> list[tuple[str, pathlib.Path, pathlib.Path, list[str]]]:
    jobs = [
        (
            "",
            worktree.path,
            worktree.repo,
            transfer.changed_files(worktree.path) + transfer.locki_tmp_files(worktree.path),
        )
    ]
    for inc in worktree.include:
        inc_path = worktree.include_path(inc.name)
        if inc_path.exists():
            jobs.append((inc.name, inc_path, inc.repo, transfer.changed_files(inc_path)))
    return jobs


def _interactive_rescue(worktree: WorktreeInfo, lost: dict[str, list[str]]) -> None:
    """Show what removal would lose and let the user pull, delete, or abort."""
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    click.echo(f"{WARNING} Removing this sandbox would lose:", err=True)
    for label, lines in lost.items():
        click.echo(f"{WARNING}   {label}:", err=True)
        for line in lines:
            click.echo(f"{WARNING}     {line}", err=True)
    if any(line.lstrip().startswith("D ") or line.startswith(" D") for lines in lost.values() for line in lines):
        click.echo(f"{INFO} (file deletions cannot be pulled -- they exist only as removals)", err=True)

    action = inquirer.select(
        message="What now?",
        choices=[
            Choice(value="pull", name="Pull these files to the host repo(s) first, then remove"),
            Choice(value="delete", name="Delete anyway (lose the files)"),
            Choice(value="abort", name="Abort"),
        ],
    ).execute()
    if action == "abort":
        raise SystemExit(1)
    if action == "delete":
        return

    jobs = _rescue_jobs(worktree)
    ignored = transfer.ignored_entries(worktree.path)
    choices = [
        Choice(value=(i, rel), name=f"{label}: {rel}" if label else rel, enabled=True)
        for i, (label, _src, _dst, rels) in enumerate(jobs)
        for rel in rels
    ] + [Choice(value=(0, rel), name=f"{rel}  (gitignored)", enabled=False) for rel in ignored]
    if not choices:
        click.echo(
            f"{INFO} None of this can be pulled -- deletions, symlinks, and nested repos have no copyable file.",
            err=True,
        )
        if not inquirer.confirm(message="Remove anyway (lose them)?", default=False).execute():
            raise SystemExit(1)
        return
    offered = {c.value for c in choices}
    selected = set(inquirer.checkbox(message="Files to pull (unchecked ones are lost):", choices=choices).execute())
    picked = [
        (
            src,
            dst,
            transfer.expand_dirs(src, [rel for rel in [*rels, *(ignored if i == 0 else [])] if (i, rel) in selected]),
        )
        for i, (_, src, dst, rels) in enumerate(jobs)
    ]

    # dry-classify ALL jobs before copying anything, so an abort at the clash
    # prompt really leaves every host repo untouched
    clashes = [(dst, c) for src, dst, rels in picked for c in transfer.classify(src, dst, rels).clashes]
    policy: transfer.ClashPolicy = "abort"
    if clashes:
        dirs = [rel for dst, rel in clashes if (dst / rel).is_dir() and not (dst / rel).is_symlink()]
        click.echo(f"{WARNING} These files clash with uncommitted content on the host:", err=True)
        for _dst, rel in clashes:
            note = " (directory in the way -- cannot be overwritten)" if rel in dirs else ""
            click.echo(f"{WARNING}     {rel}{note}", err=True)
        choices = [] if dirs else [Choice(value="overwrite", name="Overwrite them too")]
        choices += [
            Choice(value="skip", name="Pull only the non-clashing files, then remove"),
            Choice(value="abort", name="Abort"),
        ]
        clash_action = inquirer.select(message="What now?", choices=choices).execute()
        if clash_action == "abort":
            raise SystemExit(1)
        policy = clash_action

    for src_root, dst_root, rels in picked:
        result = transfer.copy_files(src_root, dst_root, rels, clash_policy=policy)
        for rel in result.copied:
            click.echo(f"{SUCCESS} Pulled {rel}", err=True)
        if result.clashes and policy == "abort":
            # the host changed between the dry classification and this copy
            fail(
                f"The host repo changed while rescuing; not pulled: {', '.join(result.clashes)}."
                f" Nothing was removed -- rerun."
            )

    # a staged version differing from the working tree has no 1:1 destination:
    # pulling saved at most the working-tree copy, so losing it needs an explicit
    # OK -- unless the file was offered and deliberately unchecked (loss accepted)
    stuck = []
    for i, (label, src, _dst, _rels) in enumerate(jobs):
        for xy, path in _porcelain(src):
            if _staged_divergent(xy) and ((i, path) in selected or (i, path) not in offered):
                stuck.append(f"{label}: {path}" if label else path)
    if stuck:
        click.echo(f"{WARNING} Staged versions of these files differ from the pulled copies (only", err=True)
        click.echo(f"{WARNING} the working-tree content was pulled; the staged one cannot be):", err=True)
        for s in stuck:
            click.echo(f"{WARNING}     {s}", err=True)
        if not inquirer.confirm(message="Remove anyway (lose those staged versions)?", default=False).execute():
            raise SystemExit(1)


def remove_sandbox(worktree: WorktreeInfo, *, branches: bool, force: bool, as_json: bool) -> None:
    """Remove a sandbox, first offering to rescue anything the removal would lose."""
    if worktree.path.exists() and not force:
        unsaved = _unsaved_work(worktree)
        tmp = transfer.locki_tmp_files(worktree.path)
        if tmp:  # artifacts already pulled to the host don't count as losable
            saved = set(transfer.classify(worktree.path, worktree.repo, tmp).identical)
            tmp = [t for t in tmp if t not in saved]
        if as_json or not sys.stdin.isatty():
            if unsaved:
                fail(
                    f"Sandbox {worktree.branch} in {worktree.path} has unsaved work"
                    f" ({', '.join(unsaved)}). Commit or stash it, or use --force."
                )
            if tmp:
                click.echo(f"{WARNING} Losing .locki/tmp artifacts: {', '.join(tmp)}", err=True)
        elif unsaved or tmp:
            lost = dict(unsaved)
            if tmp:
                lost[".locki/tmp artifacts"] = tmp
            _interactive_rescue(worktree, lost)
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
            if s.path.exists() and (tmp := transfer.locki_tmp_files(s.path)):
                saved = set(transfer.classify(s.path, s.repo, tmp).identical)
                if tmp := [t for t in tmp if t not in saved]:
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
