import pathlib
import sys

import click

from locki.cmd.internal import list_incus_containers
from locki.paths import WORKTREES, WORKTREES_META
from locki.runes import INFO, WARNING
from locki.utils import (
    LIMA_ENV,
    AliasGroup,
    fail,
    format_table,
    limactl,
    live_branch,
    pretty_path,
    run_command,
    run_in_vm,
    vm_status,
)


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
        env=LIMA_ENV,
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
        env=LIMA_ENV,
        cwd="/",
    )


_PRUNE_SCRIPT = r"""
set -eu
SHARED=/var/cache/locki/containerd-content/blobs/sha256
[ -d "$SHARED" ] || { echo "0 0"; exit 0; }

REFERENCED=$(mktemp)
trap 'rm -f "$REFERENCED"' EXIT

for ctr_name in $(incus list --format=csv --columns=n,s | awk -F, '$2~/RUNNING/{print $1}'); do
  incus exec "$ctr_name" -- ctr -n moby content ls -q 2>/dev/null \
    | sed 's/^sha256://' >> "$REFERENCED" || true
  incus exec "$ctr_name" -- k3s ctr --address /run/k3s/containerd/containerd.sock \
    -n k8s.io content ls -q 2>/dev/null \
    | sed 's/^sha256://' >> "$REFERENCED" || true
done
sort -u -o "$REFERENCED" "$REFERENCED"

REMOVED=0 FREED=0
for blob in "$SHARED"/*; do
  [ -f "$blob" ] || continue
  grep -qxF "$(basename "$blob")" "$REFERENCED" && continue
  FREED=$((FREED + $(stat -c%s "$blob" 2>/dev/null || echo 0)))
  REMOVED=$((REMOVED + 1))
  rm -f "$blob"
done
echo "$REMOVED $FREED"
"""


@vm_app.command("prune", help="Remove unreferenced container image blobs from shared cache.")
def vm_prune_cmd():
    if vm_status() != "Running":
        fail("VM is not running.")

    stopped = [name for name, status in list_incus_containers() if status != "RUNNING"]
    if stopped:
        click.echo(f"{WARNING} {len(stopped)} stopped container(s) cannot be queried.", err=True)

    result = run_in_vm(["bash", "-c", _PRUNE_SCRIPT], "Pruning shared containerd blobs")

    parts = result.stdout.decode().strip().splitlines()[-1].split()
    removed, freed = int(parts[0]), int(parts[1])
    click.echo(f"{INFO} Removed {removed} blob(s) ({freed / (1024 * 1024):.1f} MiB).", err=True)
