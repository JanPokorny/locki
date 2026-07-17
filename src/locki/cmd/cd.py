import os
import pwd

import click

from locki.cmd.new import create_sandbox_worktree
from locki.utils import resolve_sandbox, sandbox_options


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

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if not sandbox.wt_path.exists():
        create_sandbox_worktree(sandbox)

    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    os.chdir(sandbox.wt_path)
    os.execvp(shell, [shell])
