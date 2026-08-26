import json

import click

from locki.runes import ERROR, INFO, SUCCESS
from locki.services.container import containers
from locki.services.worktree import worktrees
from locki.utils import fail, json_option, sandbox_options


@click.command()
@sandbox_options()
@click.option("--all", "-a", "stop_all", is_flag=True, default=False, help="Stop all running sandboxes (all repos).")
@json_option
def stop_cmd(match, interactive, stop_all, as_json):
    """Stop a sandbox's container without removing anything.

    The worktree, branches, and container all survive; the sandbox restarts on the
    next `locki ai` / `locki x`. To power off the whole VM, use `locki vm stop`.
    """
    running = {wt_id for wt_id, status in (containers.statuses() or {}).items() if status == "running"}

    if stop_all:
        if match or interactive:
            fail("--all cannot be combined with --match or --interactive.")
        by_id = {s.wt_id: s for s in worktrees.list()}
        # running orphan containers (worktree gone) are not sandboxes — the daemon reaps them
        targets = sorted(running & by_id.keys())
        none_msg = "Nothing to stop."
    else:
        worktree = worktrees.resolve(match=match, interactive=interactive, create="deny")
        by_id = {worktree.wt_id: worktree}
        targets = [worktree.wt_id] if worktree.wt_id in running else []
        none_msg = f"{worktree.branch} is already stopped."

    if not targets:
        # nothing running is the desired end state, not an error
        click.echo(f"{INFO} {none_msg}", err=True)
        if as_json:
            click.echo(json.dumps([]))
        return

    failed = containers.stop(*targets)
    stopped = [wt_id for wt_id in targets if wt_id not in failed]
    for wt_id in stopped:
        click.echo(f"{SUCCESS} Stopped {by_id[wt_id].branch}", err=True)
    for wt_id in sorted(failed):
        click.echo(f"{ERROR} Failed to stop {by_id[wt_id].branch}.", err=True)
    if as_json:
        click.echo(json.dumps([by_id[wt_id].as_dict() for wt_id in stopped]))
    if failed:
        raise SystemExit(1)
