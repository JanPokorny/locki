import json

import click

from locki.runes import INFO
from locki.utils import cwd_git_repo, format_table, json_option, list_sandboxes, pretty_path


@click.command()
@click.option("--all", "-a", "show_all", is_flag=True, help="List sandboxes from all repos.")
@json_option
def list_cmd(show_all: bool, as_json: bool) -> None:
    """List Locki sandboxes (current repo by default; all repos outside a git repo)."""
    cwd_repo = cwd_git_repo()
    show_all = show_all or cwd_repo is None
    sandboxes = list_sandboxes()

    if not show_all:
        assert cwd_repo is not None
        sandboxes = [s for s in sandboxes if s.repo.resolve() == cwd_repo.resolve()]

    if as_json:
        click.echo(json.dumps([dict(s) for s in sandboxes]))
        return

    if not sandboxes:
        if show_all:
            click.echo(f"{INFO} No Locki sandboxes found.", err=True)
        else:
            click.echo(
                f"{INFO} No Locki sandboxes found in this repo. Add {click.style('--all', fg='green')} to look in all repos.",
                err=True,
            )
        return

    has_includes = any(s.include for s in sandboxes)

    rows: list[tuple[str, ...]] = []
    for s in sandboxes:
        row = [s.wt_id, s.branch, pretty_path(s.wt_path)]
        if show_all:
            row.append(pretty_path(s.repo))
        if has_includes:
            row.append(",".join(pretty_path(i.repo) for i in s.include) if s.include else "")
        rows.append(tuple(row))

    headers_list = ["WORKTREE ID", "WORKTREE BRANCH", "WORKTREE DIRECTORY"]
    if show_all:
        headers_list.append("PARENT REPO")
    if has_includes:
        headers_list.append("INCLUDED REPOS")

    click.echo(format_table(tuple(headers_list), rows))
