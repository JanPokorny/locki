"""Copy files between a sandbox worktree and the host repo, 1:1 by relative path.

Host-only by design: these commands write into the host repo (pull) or the
worktree (push) and are deliberately NOT part of the sandbox command bridge
(docs/adr/0001-transfer-commands-are-host-only.md).
"""

import json
import pathlib
import sys
import typing

import click

from locki.runes import INFO, SUCCESS
from locki.services import transfer
from locki.services.worktree import worktrees
from locki.utils import AliasGroup, fail, json_option, pretty_path, sandbox_options


@click.group("file", cls=AliasGroup)
def file_app():
    """Copy files between a sandbox worktree and the host repo (1:1 paths)."""


def _transfer(
    src_root: pathlib.Path,
    dst_root: pathlib.Path,
    paths: tuple[str, ...],
    *,
    force: bool,
    as_json: bool,
    action: str,
    past: str,
    json_key: str,
    allow_locki_tmp: bool,
    outside: typing.Sequence[pathlib.Path] = (),
) -> None:
    def emit_json(result: transfer.CopyResult) -> None:
        if as_json:
            click.echo(json.dumps({json_key: result.copied, "identical": result.identical}))

    if paths or outside:
        rels = (
            transfer.expand_dirs(src_root, transfer.normalize_paths(paths, src_root, allow_locki_tmp=allow_locki_tmp))
            if paths
            else []
        )
    else:
        pre_checked = transfer.changed_files(src_root)
        unchecked = transfer.locki_tmp_files(src_root) if allow_locki_tmp else []
        if not pre_checked and not unchecked:
            click.echo(f"{INFO} Nothing to {action} in {pretty_path(src_root)}.", err=True)
            emit_json(transfer.CopyResult())
            return
        if not sys.stdin.isatty():
            fail("No paths given. Pass paths explicitly in non-interactive mode.")
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice

        choices = [Choice(value=p, name=p, enabled=p in pre_checked) for p in [*pre_checked, *unchecked]]
        rels = inquirer.checkbox(message="Select files to transfer:", choices=choices).execute()
    if not rels and not outside:
        click.echo(f"{INFO} Nothing selected.", err=True)
        emit_json(transfer.CopyResult())
        return

    result = transfer.copy_files(
        src_root, dst_root, rels, clash_policy="overwrite" if force else "abort", outside=outside
    )
    if result.clashes:
        listing = "\n".join(f"     {p}" for p in result.clashes)
        fail(
            f"These files hold uncommitted content in {pretty_path(dst_root)} that would be lost"
            f" (nothing was copied):\n{listing}\n{click.style('--force', fg='green')} overwrites them."
        )

    for rel in result.copied:
        click.echo(f"{SUCCESS} {past} {rel}", err=True)
    if result.identical:
        click.echo(f"{INFO} {len(result.identical)} file(s) already identical.", err=True)
    if any(rel.startswith(".locki/tmp/") for rel in result.copied):
        click.echo(
            f"{INFO} .locki/tmp artifacts landed untracked under {pretty_path(dst_root / '.locki' / 'tmp')}.", err=True
        )
    emit_json(result)


@file_app.command("pull")
@sandbox_options()
@click.option("--force", "-f", is_flag=True, default=False, help="Overwrite files holding uncommitted content.")
@json_option
@click.argument("paths", nargs=-1)
def file_pull_cmd(match, interactive, force, as_json, paths):
    """Copy files from a sandbox worktree into the host repo (same relative paths).

    \b
    Examples:
      locki file pull                          # pick sandbox and files interactively
      locki file pull tools/gen.py             # specific file, sandbox from cwd/picker
      locki file pull -m feat .locki/tmp/shot.png
    """
    worktree = worktrees.resolve(match=match, interactive=interactive, create="deny")
    _transfer(
        worktree.path,
        worktree.repo,
        paths,
        force=force,
        as_json=as_json,
        action="pull",
        past="Pulled",
        json_key="pulled",
        allow_locki_tmp=True,
    )


@file_app.command("push")
@sandbox_options()
@click.option("--force", "-f", is_flag=True, default=False, help="Overwrite files holding uncommitted content.")
@json_option
@click.argument("paths", nargs=-1)
def file_push_cmd(match, interactive, force, as_json, paths):
    """Copy files from the host repo into a sandbox worktree (same relative paths).

    \b
    Examples:
      locki file push                          # pick sandbox and files interactively
      locki file push data/dump.json           # specific file, sandbox from cwd/picker
      locki file push -m feat downloads/
      locki file push /tmp/handoff.md          # outside the repo: lands in .locki/tmp/
    """
    worktree = worktrees.resolve(match=match, interactive=interactive, create="deny")
    # files outside the host repo have no 1:1 path; they land in .locki/tmp/<name>
    inside: list[str] = []
    outside: list[pathlib.Path] = []
    repo_real = worktree.repo.resolve()
    for raw in paths:
        p = pathlib.Path(raw).expanduser()
        candidates = [p] if p.is_absolute() else [worktree.repo / p, pathlib.Path.cwd() / p]
        full = next((c for c in candidates if c.exists()), None)
        if full is None or full.resolve().is_relative_to(repo_real):
            inside.append(raw)  # a 1:1 path (or let normalize_paths report the error)
        elif not full.resolve().is_file():
            fail(f"Only regular files can be pushed from outside the repo: {raw}.")
        else:
            outside.append(full.resolve())
    _transfer(
        worktree.repo,
        worktree.path,
        tuple(inside),
        force=force,
        as_json=as_json,
        action="push",
        past="Pushed",
        json_key="pushed",
        allow_locki_tmp=False,
        outside=outside,
    )
