import click

from locki.paths import HOME, WORKTREES
from locki.utils import cwd_git_repo, list_sandboxes


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

    def short_path(p):
        s = str(p)
        if p.is_relative_to(HOME):
            s = "~/" + str(p.relative_to(HOME))
        return s

    show_repo = all_repos or cwd_repo is None
    include_count = any(s.includes for s in sandboxes)

    rows: list[tuple[str, ...]] = []
    headers: tuple[str, ...]
    for s in sandboxes:
        row = [s.branch]
        if show_repo:
            row.append(s.repo.name)
        if include_count:
            row.append("+" + ",".join(i.name for i in s.includes) if s.includes else "")
        row.append(short_path(WORKTREES / s.wt_id))
        rows.append(tuple(row))

    headers_list = ["BRANCH"]
    if show_repo:
        headers_list.append("REPO")
    if include_count:
        headers_list.append("INCLUDES")
    headers_list.append("PATH")
    headers = tuple(headers_list)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    click.echo(fmt.format(*headers))
    for row in rows:
        click.echo(fmt.format(*row))
