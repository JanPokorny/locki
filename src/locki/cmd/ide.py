import shlex
import subprocess

import click

from locki.cmd.new import create_sandbox_worktree
from locki.config import load_config
from locki.paths import USER_CONFIG
from locki.utils import cwd_git_repo, fail, resolve_sandbox, sandbox_options


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

    ide_command = load_config(cwd_git_repo()).ide_command

    if not ide_command:
        fail(
            f"No IDE configured to launch. Set {click.style('ide_command', fg='green')} in {click.style(str(USER_CONFIG), fg='green')}"
            f" or run {click.style('locki setup', fg='green')}."
        )

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if not sandbox.wt_path.exists():
        create_sandbox_worktree(sandbox)

    try:
        subprocess.run(shlex.split(ide_command), cwd=str(sandbox.wt_path))
    except FileNotFoundError:
        executable = shlex.split(ide_command)[0]
        fail(f"IDE command '{executable}' not found. Is it installed and on your PATH?")
