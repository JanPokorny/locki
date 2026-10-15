import shlex
import subprocess

import click

from locki.cmd.setup import ensure_configured
from locki.paths import USER_CONFIG
from locki.services.worktree import worktrees
from locki.utils import fail, sandbox_options


@click.command("ide")
@sandbox_options(create=True)
def ide_cmd(match, interactive, create, dirty, raw):
    """Open an IDE in a sandbox worktree.

    \b
    Examples:
      locki ide                       # current sandbox / picker / create, open editor
      locki ide -m feat               # open editor in matching sandbox
      locki ide -i                    # force sandbox picker
      locki ide -n                    # create new sandbox and open editor
    """

    ide_command = ensure_configured(worktrees.cwd_repo).ide_command

    if not ide_command:
        fail(
            f"No IDE configured to launch. Set {click.style('ide_command', fg='green')} in {click.style(str(USER_CONFIG), fg='green')}"
            f" or run {click.style('locki setup', fg='green')}."
        )

    create = create or ((dirty or raw) and not match and not interactive)  # --dirty/--raw can only seed a new sandbox
    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    worktrees.ensure_created(worktree, dirty=dirty, raw=raw)

    argv = shlex.split(ide_command)
    try:
        subprocess.run(argv, cwd=str(worktree.path))
    except FileNotFoundError:
        fail(f"IDE command '{argv[0]}' not found. Is it installed and on your PATH?")
