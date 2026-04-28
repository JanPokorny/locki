import click

from locki.paths import WORKTREES
from locki.utils import cwd_git_repo, format_table, list_sandboxes, pretty_path


@click.command()
@click.option("-a", "--all", "all_repos", is_flag=True, default=False, help="Show sandboxes from all repos.")
def list_cmd(all_repos):
    """List Locki sandboxes (current repo by default; all repos outside a git repo or with --all)."""
    cwd_repo = cwd_git_repo()
    sandboxes = list_sandboxes()

    if not all_repos and cwd_repo is not None:
        sandboxes = [s for s in sandboxes if s.repo.resolve() == cwd_repo.resolve()]

    if not sandboxes:
        if all_repos or cwd_repo is None:
            click.echo("No Locki sandboxes found.")
        else:
            click.echo("No Locki sandboxes in this repo. (use --all to see all repos)")
        return

    has_includes = any(s.include for s in sandboxes)
    show_repo = all_repos or cwd_repo is None

    rows: list[tuple[str, ...]] = []
    for s in sandboxes:
        row = [s.wt_id, s.branch, pretty_path(WORKTREES / s.wt_id)]
        if show_repo:
            row.append(pretty_path(s.repo))
        if has_includes:
            row.append(",".join(pretty_path(i.repo) for i in s.include) if s.include else "")
        rows.append(tuple(row))

    headers_list = ["WORKTREE ID", "WORKTREE BRANCH", "WORKTREE DIRECTORY"]
    if show_repo:
        headers_list.append("PARENT REPO")
    if has_includes:
        headers_list.append("INCLUDED REPOS")

    click.echo(format_table(tuple(headers_list), rows))
