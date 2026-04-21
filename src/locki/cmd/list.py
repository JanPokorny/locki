import pathlib

import click

from locki.paths import HOME, WORKTREES
from locki.utils import git_root, run_command


@click.command()
def list_cmd():
    """List Locki worktrees in the current repo."""
    repo_root = git_root()
    result = run_command(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        "Listing worktrees",
        quiet=True,
    )

    rows: list[tuple[str, str]] = []
    current_path: pathlib.Path | None = None
    current_branch: str | None = None
    for line in result.stdout.decode().splitlines():
        if line.startswith("worktree "):
            current_path = pathlib.Path(line.split(" ", 1)[1]).resolve()
            current_branch = None
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
        elif line == "" and current_path and current_branch:
            if current_path.is_relative_to(WORKTREES):
                path_str = str(current_path)
                if current_path.is_relative_to(HOME):
                    path_str = "~/" + str(current_path.relative_to(HOME))
                rows.append((current_branch, path_str))

    if not rows:
        click.echo("No Locki worktrees in this repo.")
        return

    # Compute column widths
    headers = ("BRANCH", "PATH")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    fmt = f"{{:<{widths[0]}}}  {{}}"
    click.echo(fmt.format(*headers))
    for row in rows:
        click.echo(fmt.format(*row))
