import pathlib
import sys

import click

from locki.paths import WORKTREES, WORKTREES_META
from locki.utils import AliasGroup, format_table, limactl, live_branch, pretty_path, run_command, run_in_vm, vm_status


@click.group(cls=AliasGroup, help="Manage the Locki VM.")
def vm_app():
    pass


@vm_app.command("status | st", help="Show VM and sandbox status.")
def vm_status_cmd():
    status = (vm_status() or "none").lower()
    click.echo(f"VM: {status}")

    if status != "running":
        return

    try:
        result = run_in_vm(
            ["incus", "list", "--format=csv", "--columns=n,s"],
            "Listing containers",
            check=False,
            quiet=True,
        )
    except Exception:
        return

    rows: list[tuple[str, str, str, str, str]] = []
    for line in result.stdout.decode().splitlines():
        parts = line.split(",", 1)
        if len(parts) != 2:
            continue
        wt_id = parts[0].strip()
        status = parts[1].strip().lower()
        meta_dir = next(WORKTREES_META.glob(f"*{wt_id}"))
        repo_file = meta_dir / "repo"
        branch = live_branch(meta_dir) if meta_dir.is_dir() else ""
        rows.append(
            (
                wt_id,
                status,
                pretty_path(pathlib.Path(repo_file.read_text().strip() if repo_file.exists() else "")),
                branch,
                pretty_path(next(WORKTREES.glob(f"*{wt_id}"))),
            )
        )

    if not rows:
        click.echo("No sandboxes.")
        return

    headers = ("SANDBOX ID", "STATUS", "REPO", "BRANCH", "WORKTREE")
    click.echo(format_table(headers, sorted(rows, key=lambda r: (r[1], r[2], r[3]))))


@vm_app.command("stop", help="Stop the Locki VM.")
def vm_stop_cmd():
    run_command(
        [limactl(), "stop", "locki"],
        "Stopping VM",
        cwd="/",
    )


@vm_app.command("delete | remove | rm", help="Delete the Locki VM entirely.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
def vm_delete_cmd(yes):
    if not yes:
        click.echo(
            "Warning: Deleting the VM will stop all current sandboxes. Worktree and home data won't be lost. Sandboxes may need to reinstall dependencies after reopening."
        )
        if not sys.stdin.isatty():
            click.echo("Pass --yes to accept this warning.")
            raise SystemExit(1)
        click.confirm("Continue?", abort=True)
    run_command(
        [limactl(), "delete", "-f", "locki"],
        "Deleting VM",
        cwd="/",
    )
