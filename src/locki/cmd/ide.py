import os
import shlex
import subprocess

import click

from locki.cmd.setup import ensure_configured
from locki.paths import USER_CONFIG
from locki.services.worktree import worktrees
from locki.utils import fail, sandbox_options


@click.command("ide")
@sandbox_options(create=True)
def ide_cmd(match, interactive, create):
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

    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if not worktree.path.exists():
        worktrees.create(worktree)

    argv: str | list[str] = ide_command if os.name == "nt" else shlex.split(ide_command)
    try:
        subprocess.run(argv, cwd=str(worktree.path))
    except FileNotFoundError:
        executable = ide_command.split(maxsplit=1)[0] if isinstance(argv, str) else argv[0]
        fail(f"IDE command '{executable}' not found. Is it installed and on your PATH?")
