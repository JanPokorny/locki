import json
import pathlib
import sys

import click

from locki.runes import INFO, SUCCESS, WARNING
from locki.services.worktree import worktrees
from locki.utils import dirty_option, fail, json_option, pretty_path, raw_option, run_command


def _confirm_diverged_base(repo: pathlib.Path, from_ref: str, force: bool) -> None:
    """--dirty snapshots against HEAD; applying onto a different --from base may
    conflict (in the sandbox only). Warn and ask, or require --force headlessly."""

    def rev(ref: str) -> str:
        return (
            run_command(
                ["git", "-C", str(repo), "rev-parse", "--verify", ref], "Resolving ref", check=False, quiet=True
            )
            .stdout.decode()
            .strip()
        )

    if not rev(from_ref) or rev(from_ref) == rev("HEAD"):
        return
    click.echo(
        f"{WARNING} --dirty snapshots your changes against HEAD; applying them onto"
        f" {click.style(from_ref, fg='yellow')} may leave conflict markers in the sandbox.",
        err=True,
    )
    if force:
        return
    if not sys.stdin.isatty():
        fail("Refusing --dirty with a diverged --from base in non-interactive mode. Pass --force to proceed.")
    from InquirerPy import inquirer

    if not inquirer.confirm(message="Apply anyway?", default=True).execute():
        raise SystemExit(1)


@click.command("new | n")
@click.option("--from", "-f", "from_ref", default=None, help="Base the new branch on this ref instead of HEAD.")
@click.option("--branch", "-b", "branch_stem", default="untitled", help="Branch name stem (#locki-<id> is appended).")
@dirty_option
@raw_option
@click.option(
    "--force", is_flag=True, default=False, help="With --dirty --from: apply onto a diverged base without asking."
)
@json_option
def new_cmd(as_json: bool, from_ref: str | None, branch_stem: str, dirty: bool, raw: bool, force: bool):
    """Create a new sandbox worktree. Alternatively, pass --new to other Locki commands as a shortcut.

    \b
    Examples:
      locki new                            # create sandbox worktree
      locki new -b my-feature              # branch named my-feature#locki-<id>
      locki new -f origin/main             # branch off origin/main
      locki new --dirty                    # carry uncommitted host changes along
      locki new --raw                      # --dirty plus gitignored files (.env, ...)
      id=$(locki new --json | jq -r .id)   # capture the sandbox id in scripts
    """
    cwd_repo = worktrees.cwd_repo
    if cwd_repo is None:
        fail("Cannot create a sandbox outside a git repo.")
    if (dirty or raw) and from_ref:
        _confirm_diverged_base(cwd_repo, from_ref, force)
    worktree = worktrees.new(cwd_repo, branch_stem)
    worktrees.create(worktree, from_ref, dirty=dirty, raw=raw)
    if as_json:
        click.echo(json.dumps(worktree.as_dict()))
        return
    click.echo(f"{SUCCESS} Created sandbox {click.style(worktree.wt_id, fg='green')}.", err=True)
    click.echo(f"{INFO}    branch: {click.style(worktree.branch, fg='green')}", err=True)
    click.echo(f"{INFO}   on disk: {click.style(pretty_path(worktree.path), fg='green')}", err=True)
    click.echo(
        f"{INFO}  enter it: {click.style(f'locki x -m {worktree.wt_id}', fg='green')}"
        f" (or {click.style(f'locki ai -m {worktree.wt_id}', fg='green')})",
        err=True,
    )
