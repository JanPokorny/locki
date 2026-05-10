import shutil
import subprocess
import sys

import click

from locki.cmd.new import create_sandbox_worktree
from locki.config import load_config, save_user_config
from locki.paths import USER_CONFIG
from locki.runes import SUCCESS
from locki.utils import cwd_git_repo, fail, resolve_sandbox

EDITORS = [
    ("code", "VSCode"),
    ("zed", "Zed"),
    ("fresh", "Fresh"),
]
EDITOR_CMDS = [cmd for cmd, _ in EDITORS]


def _ask_editor() -> str:
    if not sys.stdin.isatty():
        fail(
            f"No default editor configured. "
            f"Run {click.style('locki ide', fg='green')} interactively first to pick one, "
            f"or configure e.g. {click.style('ide = "code"', fg='yellow')} in {click.style(str(USER_CONFIG), fg='cyan')}."
        )

    available = [(cmd, label) for cmd, label in EDITORS if shutil.which(cmd)]
    if not available:
        fail("No supported editor found in PATH (code, zed, fresh).")

    if len(available) == 1:
        selected = available[0][0]
    else:
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice

        selected = inquirer.select(
            message="Select your default editor:",
            choices=[Choice(value=cmd, name=label) for cmd, label in available],
        ).execute()

    save_user_config("ide", selected)
    click.echo(
        f"{SUCCESS} Saved default editor {click.style(selected, fg='green')} to {USER_CONFIG}",
        err=True,
    )
    return selected


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
    if create and (match or interactive):
        fail("--new conflicts with --match/--interactive.")

    config = load_config(cwd_git_repo())
    editor = config.ide if config.ide in EDITOR_CMDS else None
    if editor is None:
        editor = _ask_editor()

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if not sandbox.wt_path.exists():
        create_sandbox_worktree(sandbox)

    subprocess.run([editor, "."], cwd=str(sandbox.wt_path))
