"""`locki template` — reuse one sandbox's OS setup for new sandboxes of the same repo.

`set` copies the sandbox's container (as of now: installed packages, services,
Docker images, ... — not the worktree or per-sandbox caches) into a stopped template
container; from then on, new sandboxes of the repo start as copies of it instead of
the fresh image + setup.  Re-run `set` to refresh it; `unset` goes back to the image.
"""

import json
import pathlib

import click

from locki.runes import INFO, SUCCESS
from locki.services.container import containers
from locki.services.worktree import worktrees
from locki.utils import AliasGroup, fail, json_option, pretty_path, sandbox_options


@click.group(cls=AliasGroup)
def template_app():
    """Use a sandbox as the template for new sandboxes of its repo.

    \b
    Examples:
      locki template set            # current sandbox becomes this repo's template
      locki template set -m feat    # ...or the sandbox matching 'feat'
      locki template get            # show this repo's template
      locki template unset          # new sandboxes start from the image again
    """


def _cwd_repo():
    repo = worktrees.cwd_repo
    if repo is None:
        fail("Not inside a git repo.")
    return repo


@template_app.command("set")
@sandbox_options()
@json_option
def template_set_cmd(match, interactive, as_json):
    """Snapshot a sandbox's container as the template for its repo's new sandboxes."""
    worktree = worktrees.resolve(match=match, interactive=interactive, create="deny")
    info = containers.set_template(worktree)
    if as_json:
        click.echo(json.dumps(info.as_dict()))
        return
    click.echo(
        f"{SUCCESS} New sandboxes of {click.style(worktree.repo.name, fg='green')} will start"
        f" as copies of {click.style(worktree.wt_id, fg='green')} ({worktree.branch}) as of now.",
        err=True,
    )
    click.echo(
        f"{INFO} Only the container is copied; worktree contents and per-sandbox caches"
        " (node_modules, .venv, ...) are not. Re-run to refresh.",
        err=True,
    )


@template_app.command("get")
@json_option
def template_get_cmd(as_json):
    """Show the template for the current repo's new sandboxes."""
    info = containers.template(_cwd_repo())
    if as_json:
        click.echo(json.dumps(info.as_dict() if info else None))
        return
    if info is None:
        click.echo(f"{INFO} No template set; new sandboxes start from the image.", err=True)
        return
    click.echo(f"repo:    {pretty_path(pathlib.Path(info.repo))}")
    click.echo(f"sandbox: {info.source} ({info.branch})")
    click.echo(f"taken:   {info.created} (Locki {info.version})")


@template_app.command("unset")
@json_option
def template_unset_cmd(as_json):
    """Remove the current repo's template; new sandboxes start from the image again."""
    info = containers.unset_template(_cwd_repo())
    if as_json:
        click.echo(json.dumps(info.as_dict() if info else None))
        return
    if info is None:
        click.echo(f"{INFO} No template set.", err=True)
        return
    click.echo(f"{SUCCESS} Template removed; new sandboxes start from the image again.", err=True)
