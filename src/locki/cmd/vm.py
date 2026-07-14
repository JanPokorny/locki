import json
import pathlib
import shlex
import sys

import click

from locki.paths import WORKTREES, WORKTREES_META
from locki.runes import INFO
from locki.utils import (
    LIMA_ENV,
    AliasGroup,
    fail,
    format_table,
    json_option,
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
@json_option
def vm_status_cmd(as_json):
    status = (vm_status() or "none").lower()

    sandboxes: list[dict] = []
    if status == "running":
        result = run_in_vm(
            ["incus", "list", "--format=csv", "--columns=n,s"],
            "Listing containers",
            check=False,
            quiet=True,
        )
        for line in result.stdout.decode().splitlines():
            wt_id, sep, container_status = line.partition(",")
            if not sep:
                continue
            wt_id = wt_id.strip()
            meta_dir = next(WORKTREES_META.glob(f"*{wt_id}"), None)
            wt_path = next(WORKTREES.glob(f"*{wt_id}"), None)
            repo_file = meta_dir / "repo" if meta_dir else None
            sandboxes.append(
                {
                    "id": wt_id,
                    "status": container_status.strip().lower(),
                    "repo": repo_file.read_text().strip() if repo_file and repo_file.exists() else "",
                    "branch": live_branch(meta_dir) if meta_dir else "",
                    "worktree": str(wt_path) if wt_path else "",
                }
            )

    if as_json:
        click.echo(json.dumps({"vm": status, "sandboxes": sandboxes}))
        return

    click.echo(f"VM: {status}")
    if status != "running":
        return
    if not sandboxes:
        click.echo("No sandboxes.")
        return

    rows = [
        (
            s["id"],
            s["status"],
            pretty_path(pathlib.Path(s["repo"])) if s["repo"] else "",
            s["branch"],
            pretty_path(pathlib.Path(s["worktree"])) if s["worktree"] else "",
        )
        for s in sandboxes
    ]
    headers = ("SANDBOX ID", "STATUS", "REPO", "BRANCH", "WORKTREE")
    click.echo(format_table(headers, sorted(rows, key=lambda r: (r[1], r[2], r[3]))))


@vm_app.command("stop", help="Stop the Locki VM.")
def vm_stop_cmd():
    run_command(
        [limactl(), "stop", "-f", "locki"],
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
CACHE=/var/cache/locki
WORKTREES=__WORKTREES__

size() { du -sb "$@" 2>/dev/null | awk '{s+=$1} END {print s+0}'; }

BEFORE=$(size "$CACHE/registry-cache" "$CACHE/scoped")

if [ -d "$CACHE/registry-cache" ]; then
  find "$CACHE/registry-cache" -mindepth 1 -delete 2>/dev/null || true
  systemctl restart nginx 2>/dev/null || true
fi

# Sandbox-scoped caches live under scoped/<wt-id>/; drop entries whose sandbox
# worktree no longer exists (the worktrees dir is mounted in the VM at the host path).
if [ -d "$CACHE/scoped" ]; then
  for dir in "$CACHE/scoped"/*; do
    [ -e "$dir" ] || continue
    ls -d "$WORKTREES"/*"-locki-$(basename "$dir")" >/dev/null 2>&1 || rm -rf "$dir"
  done
fi

AFTER=$(size "$CACHE/registry-cache" "$CACHE/scoped")
FREED=$((BEFORE - AFTER))
[ "$FREED" -lt 0 ] && FREED=0
echo "$FREED"
"""


@vm_app.command("prune", help="Clear the registry cache and caches of removed sandboxes.")
@json_option
def vm_prune_cmd(as_json):
    if vm_status() != "Running":
        fail("VM is not running.")

    result = run_in_vm(
        ["bash", "-c", _PRUNE_SCRIPT.replace("__WORKTREES__", shlex.quote(str(WORKTREES)))], "Pruning caches"
    )

    freed = int(result.stdout.decode().strip().splitlines()[-1])
    if as_json:
        click.echo(json.dumps({"freed_bytes": freed}))
        return
    click.echo(f"{INFO} Freed {freed / (1024 * 1024):.1f} MiB from caches.", err=True)
