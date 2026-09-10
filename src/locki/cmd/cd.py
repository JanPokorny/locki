import os
import pwd

import click

from locki.services.worktree import worktrees
from locki.utils import sandbox_options


@click.command("cd")
@sandbox_options(create=True)
def cd_cmd(match, interactive, create, dirty, raw):
    """Open a local shell in a worktree.

    The shell runs on host -- to run a sandboxed shell, use `locki x`.

    \b
    Examples:
      locki cd                        # current sandbox / picker / create, open shell
      locki cd -m feat                # open shell in matching sandbox
      locki cd -i                     # force sandbox picker
      locki cd -n                     # create new sandbox and open shell
    """

    create = create or ((dirty or raw) and not match and not interactive)  # --dirty/--raw can only seed a new sandbox
    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    worktrees.ensure_created(worktree, dirty=dirty, raw=raw)

    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    os.chdir(worktree.path)
    os.execvp(shell, [shell])
