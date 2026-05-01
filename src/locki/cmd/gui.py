"""Forward a Wayland GUI app from a sandbox to the host (experimental).

Pipeline:

    macOS                                              Lima VM / Incus container
    ┌────────────────────────────────────────┐         ┌──────────────────────────────┐
    │ cocoa-way (Wayland compositor)         │         │                              │
    │   ▲                                    │         │ waypipe server -- <gui-app>  │
    │ waypipe-darwin client (UNIX socket)    │         │   ▼ connects to UNIX socket  │
    │   ▲ socat TCP-LISTEN:PORT ─────────────┼─── TCP ─┼─── socat UNIX-LISTEN ◀───────│
    └────────────────────────────────────────┘         └──────────────────────────────┘

The container reaches the macOS host via ``host.lima.internal`` (already in
``/etc/hosts`` from container-setup.sh).  We don't use Locki's host→sandbox
port-forward here because the data direction is sandbox→host.

Setup on macOS (one-time):
    brew tap J-x-Z/tap && brew install cocoa-way waypipe-darwin socat

See https://github.com/J-x-Z/cocoa-way for the host-side compositor.
"""

import shlex
import subprocess

import click

from locki.cmd.exec import CONTAINER_ENV
from locki.runes import INFO
from locki.utils import fail, limactl, resolve_sandbox, run_in_vm


@click.command(
    "gui",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("-m", "--match", default=None, help="Substring match on existing sandbox branch.")
@click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("-p", "--port", type=int, default=9001, help="TCP port for the Wayland bridge (host-side listener).")
@click.option(
    "--host-addr",
    default="host.lima.internal",
    help="Address the sandbox uses to reach the host (default: host.lima.internal).",
)
@click.pass_context
def gui_cmd(ctx, match, interactive, port, host_addr):
    """Run a GUI app from a sandbox, forwarding Wayland to the host (experimental).

    Requires cocoa-way + waypipe-darwin on macOS hosts:
        brew tap J-x-Z/tap && brew install cocoa-way waypipe-darwin socat

    \b
    Examples:
      locki gui firefox
      locki gui -m feat firefox
      locki gui -- xeyes
    """
    if not ctx.args:
        fail(f"Specify a GUI command, e.g. {click.style('locki gui firefox', fg='green')}.")
    cmd_in_sandbox = " ".join(shlex.quote(a) for a in ctx.args)

    sandbox = resolve_sandbox(match=match, interactive=interactive, create="deny")

    result = run_in_vm(
        ["incus", "list", "--format=csv", "--columns=ns", sandbox.wt_id],
        "Checking sandbox",
        check=False,
    )
    lines = result.stdout.decode().strip()
    if sandbox.wt_id not in lines:
        match_arg = f" -m {match}" if match else ""
        fail(
            f"Sandbox not found. Start one first: "
            f"{click.style(f'locki x{match_arg} bash', fg='green')}."
        )
    if "RUNNING" not in lines:
        fail(
            f"Sandbox is not running. Run "
            f"{click.style(f'locki x -m {sandbox.wt_id} true', fg='green')} to start it."
        )

    install_script = (
        "set -e; "
        "missing=; "
        'command -v waypipe >/dev/null 2>&1 || missing="$missing waypipe"; '
        'command -v socat   >/dev/null 2>&1 || missing="$missing socat"; '
        'command -v Xwayland >/dev/null 2>&1 || missing="$missing xorg-x11-server-Xwayland"; '
        'if [ -n "$missing" ]; then '
        "  if command -v dnf >/dev/null 2>&1; then "
        "    dnf install -y --setopt install_weak_deps=False $missing >/dev/null; "
        "  elif command -v apt-get >/dev/null 2>&1; then "
        "    pkgs=$(echo \"$missing\" | sed 's/xorg-x11-server-Xwayland/xwayland/g'); "
        "    apt-get update -qq && apt-get install -y $pkgs >/dev/null; "
        "  else "
        "    echo 'No supported package manager; install waypipe + socat manually.' >&2; exit 1; "
        "  fi; "
        "fi"
    )
    run_in_vm(
        ["incus", "exec", sandbox.wt_id, "--", "bash", "-c", install_script],
        "Ensuring waypipe + socat installed in sandbox",
    )

    click.echo()
    click.echo(f"{INFO} Starting Wayland bridge from sandbox to {click.style(host_addr, fg='cyan')}:{port}.")
    click.echo(f"{INFO} On the macOS host, run these (in this order, separate terminals):")
    click.echo()
    click.echo(click.style("  # one-time:", dim=True))
    click.echo(click.style("  brew tap J-x-Z/tap && brew install cocoa-way waypipe-darwin socat", fg="cyan"))
    click.echo()
    click.echo(click.style("  # each session:", dim=True))
    click.echo(click.style("  cocoa-way &", fg="cyan"))
    click.echo(click.style("  waypipe-darwin --socket /tmp/locki-wp.sock client &", fg="cyan"))
    click.echo(
        click.style(
            f"  socat TCP-LISTEN:{port},reuseaddr,bind=0.0.0.0,fork UNIX-CONNECT:/tmp/locki-wp.sock",
            fg="cyan",
        )
    )
    click.echo()
    click.echo(f"{INFO} If macOS prompts about incoming connections, allow them for socat.")
    click.echo(f"{INFO} Press Ctrl-C here to stop the GUI app and tear down the bridge.")
    click.echo()

    sandbox_script = (
        "set -e; "
        "trap 'kill 0 2>/dev/null' EXIT; "
        "rm -f /tmp/locki-wp.sock; "
        f"socat UNIX-LISTEN:/tmp/locki-wp.sock,fork TCP:{host_addr}:{port} & "
        "for _ in $(seq 1 20); do [ -S /tmp/locki-wp.sock ] && break; sleep 0.1; done; "
        f"exec waypipe --socket /tmp/locki-wp.sock server -- {cmd_in_sandbox}"
    )

    env_flags = [flag for k, v in CONTAINER_ENV.items() for flag in ("--env", f"{k}={v}")]
    result = subprocess.run(
        [
            limactl(),
            "shell",
            "--yes",
            "--start",
            "--workdir=/",
            "locki",
            "--",
            "sudo",
            "incus",
            "exec",
            sandbox.wt_id,
            "--cwd",
            str(sandbox.wt_path),
            *env_flags,
            "--",
            "bash",
            "-c",
            sandbox_script,
        ],
    )

    raise SystemExit(result.returncode)
