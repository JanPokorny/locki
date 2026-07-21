import sys

import click

from locki.paths import WORKTREES
from locki.runes import EXIT, INFO, SPINNER
from locki.services.container import containers
from locki.services.daemon import daemon
from locki.services.vm import vm
from locki.services.worktree import WorktreeInfo, worktrees
from locki.utils import pretty_path, sandbox_options


@click.command(
    "exec | x",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "allow_interspersed_args": False},
)
@sandbox_options(create=True)
@click.pass_context
def exec_cmd(ctx, match, interactive, create):
    """Run a command in the per-branch sandbox container.

    \b
    Examples:
      locki x bash                    # current sandbox, or picker / create
      locki x claude                  # run Claude Code
      locki x -m feat bash            # match sandbox by substring
      locki x -i bash                 # force sandbox picker even inside a worktree
      locki x -n bash                 # create new sandbox
      locki x bash -c "echo hello"    # run a one-liner
    """
    click.echo(f"{SPINNER} Entering a Locki sandbox.", err=True)

    pre_resolved = ctx.obj if isinstance(ctx.obj, WorktreeInfo) else None
    worktree = pre_resolved or worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    WORKTREES.mkdir(parents=True, exist_ok=True)
    worktrees.prepare_home(worktree)

    vm.ensure_running()

    if not worktree.wt_path.exists():
        worktrees.create(worktree)
    else:
        worktrees.fix_branches(worktree)

    containers.ensure_running(worktree)
    daemon.ensure_running()

    result = containers.exec_interactive(worktree, ctx.args or ["bash"])

    clear = "\r\033[2K" if sys.stderr.isatty() else ""
    click.echo(clear, err=True)
    click.echo(f"{clear}{EXIT} Exited Locki sandbox.", err=True)
    click.echo(f"{clear}{INFO} Return to this sandbox:", err=True)
    click.echo(
        f"{clear}{INFO}      via AI: {click.style(f'locki ai -m {worktree.wt_id}', fg='green')}"
        f" (or just {click.style('locki ai', fg='green')} and find it in the list)",
        err=True,
    )
    click.echo(
        f"{clear}{INFO}   via shell: {click.style(f'locki x -m {worktree.wt_id}', fg='green')}",
        err=True,
    )
    click.echo(f"{clear}{INFO}     on disk: {click.style(pretty_path(worktree.wt_path), fg='green')}", err=True)
    raise SystemExit(result.returncode)
