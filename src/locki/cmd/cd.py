import os
import pwd

import click

from locki.services.worktree import worktrees
from locki.utils import sandbox_options


@click.command("cd")
@sandbox_options(create=True)
def cd_cmd(match, interactive, create):
    """Open a local shell in a worktree.

    The shell runs on host -- to run a sandboxed shell, use `locki x`.

    \b
    Examples:
      locki cd                        # current sandbox / picker / create, open shell
      locki cd -m feat                # open shell in matching sandbox
      locki cd -i                     # force sandbox picker
      locki cd -n                     # create new sandbox and open shell
    """

    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if not worktree.path.exists():
        worktrees.create(worktree)

    # cd never touches the VM, so stamp here or host-side work reads as stale
    worktrees.touch(worktree.wt_id)
    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    os.chdir(worktree.path)
    os.execvp(shell, [shell])
