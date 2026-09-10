import sys
import typing

import click

from locki.paths import WORKTREES
from locki.runes import EXIT, INFO, SPINNER
from locki.services.container import containers
from locki.services.daemon import daemon
from locki.services.home import home
from locki.services.vm import vm
from locki.services.worktree import WorktreeInfo, worktrees
from locki.utils import CLEAR_LINE, check_dirty_applies, pretty_path, sandbox_options


def enter_sandbox(
    worktree: WorktreeInfo, command: list[str], *, dirty: bool = False, raw: bool = False
) -> typing.NoReturn:
    """Bring up everything a sandbox needs (home, VM, worktree, container, daemon),
    run *command* in it interactively, and exit with its return code."""
    check_dirty_applies(dirty or raw, worktree.path.exists())  # fail fast, before the VM spin-up

    click.echo(f"{SPINNER} Entering a Locki sandbox.", err=True)

    WORKTREES.mkdir(parents=True, exist_ok=True)
    home.prepare(worktree.path)

    vm.ensure_running()

    if not worktrees.ensure_created(worktree, dirty=dirty, raw=raw):
        worktrees.fix_branches(worktree)

    containers.ensure_running(worktree)
    daemon.ensure_running()

    result = containers.exec_interactive(worktree, command)

    # blank separator; on a TTY the clear also wipes leftover container output on the current line
    click.echo(CLEAR_LINE if sys.stderr.isatty() else "", err=True)
    click.echo(f"{EXIT} Exited Locki sandbox.", err=True)
    click.echo(f"{INFO} Return to this sandbox:", err=True)
    click.echo(
        f"{INFO}      via AI: {click.style(f'locki ai -m {worktree.wt_id}', fg='green')}"
        f" (or just {click.style('locki ai', fg='green')} and find it in the list)",
        err=True,
    )
    click.echo(
        f"{INFO}   via shell: {click.style(f'locki x -m {worktree.wt_id}', fg='green')}",
        err=True,
    )
    click.echo(f"{INFO}     on disk: {click.style(pretty_path(worktree.path), fg='green')}", err=True)
    raise SystemExit(result.returncode)


@click.command(
    "exec | x",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "allow_interspersed_args": False},
)
@sandbox_options(create=True)
@click.pass_context
def exec_cmd(ctx, match, interactive, create, dirty, raw):
    """Run a command in the per-branch sandbox container.

    \b
    Examples:
      locki x bash                    # current sandbox, or picker / create
      locki x claude                  # run Claude Code
      locki x -m feat bash            # match sandbox by substring
      locki x -i bash                 # force sandbox picker even inside a worktree
      locki x -n bash                 # create new sandbox
      locki x -n --dirty bash         # new sandbox carrying uncommitted host changes
      locki x bash -c "echo hello"    # run a one-liner
    """
    create = create or ((dirty or raw) and not match and not interactive)  # --dirty/--raw can only seed a new sandbox
    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )
    enter_sandbox(worktree, ctx.args or ["bash"], dirty=dirty, raw=raw)
