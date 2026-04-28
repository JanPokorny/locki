import json
import pathlib

import click

from locki.paths import LIMA, WORKTREES, WORKTREES_META
from locki.utils import AliasGroup, format_table, limactl, live_branch, pretty_path, run_command, run_in_vm


@click.group(cls=AliasGroup, help="Manage the Locki VM.")
def vm_app():
    pass


@vm_app.command("status | st", help="Show VM and sandbox status.")
def vm_status_cmd():
    vm_status = "none"
    try:
        result = run_command(
            [limactl(), "list", "--json"],
            "Checking VM",
            env={"LIMA_HOME": str(LIMA)},
            cwd="/",
            check=False,
            quiet=True,
        )
        for line in result.stdout.decode().splitlines():
            vm = json.loads(line)
            if vm.get("name") == "locki":
                vm_status = vm.get("status", "unknown").lower()
    except Exception:
        pass

    click.echo(f"VM: {vm_status}")

    if vm_status != "running":
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
        meta_dir = WORKTREES_META / wt_id
        repo_file = meta_dir / "repo"
        branch = live_branch(meta_dir) if meta_dir.is_dir() else ""
        repo_path = pathlib.Path(repo_file.read_text().strip()) if repo_file.exists() else None
        rows.append((
            wt_id,
            status,
            pretty_path(repo_path) if repo_path else "",
            branch,
            pretty_path(WORKTREES / wt_id),
        ))

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
        env={"LIMA_HOME": str(LIMA)},
        cwd="/",
    )


@vm_app.command("delete | remove | rm", help="Delete the Locki VM entirely.")
def vm_delete_cmd():
    run_command(
        [limactl(), "delete", "-f", "locki"],
        "Deleting VM",
        env={"LIMA_HOME": str(LIMA)},
        cwd="/",
    )
