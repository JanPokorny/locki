import shlex
import subprocess

import click

from locki.cmd.new import create_sandbox_worktree
from locki.config import load_config
from locki.utils import cwd_git_repo, resolve_sandbox


@click.command("ide")
@click.option("-m", "--match", default=None, help="Substring match on existing sandbox branch.")
@click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("-n", "--new", "create", is_flag=True, default=False, help="Create a new sandbox.")
def ide_cmd(match, interactive, create):
    """Open an IDE in a sandbox worktree.

    \b
    Examples:
      locki ide                       # current sandbox / picker / create, open editor
      locki ide -m feat               # open editor in matching sandbox
      locki ide -i                    # force sandbox picker
      locki ide -n                    # create new sandbox and open editor
    """

    ide_command = load_config(cwd_git_repo()).ide_command

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if not sandbox.wt_path.exists():
        create_sandbox_worktree(sandbox)

    subprocess.run(shlex.split(ide_command), cwd=str(sandbox.wt_path))
