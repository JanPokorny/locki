import socket

import click

from locki.utils import fail, resolve_sandbox, run_in_vm, sandbox_options


def _parse_port_spec(spec: str) -> tuple[int, int]:
    """Parse port spec into (host_port, sandbox_port). Host port 0 means random."""
    parts = spec.split(":")
    if len(parts) == 1:
        port = int(parts[0])
        return port, port
    if len(parts) == 2:
        if parts[0] == "":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", 0))
                host = s.getsockname()[1]
        else:
            host = int(parts[0])
        return host, int(parts[1])
    raise click.BadParameter(f"Invalid port spec '{spec}'. Use 'port', 'host_port:sandbox_port', or ':sandbox_port'.")


def _forward_devices(wt_id: str) -> list[str]:
    """Names of all port-forward proxy devices on a container."""
    result = run_in_vm(
        ["incus", "config", "device", "list", wt_id],
        "Listing devices",
        quiet=True,
    )
    return [name for line in result.stdout.decode().splitlines() if (name := line.strip()).startswith("port-fwd-")]


def _device_port(wt_id: str, name: str, key: str) -> str:
    """Last `:`-separated field of a proxy device address, e.g. tcp:0.0.0.0:8080 -> 8080."""
    value = (
        run_in_vm(
            ["incus", "config", "device", "get", wt_id, name, key],
            f"Reading {name}",
            check=False,
            quiet=True,
        )
        .stdout.decode()
        .strip()
    )
    return value.rsplit(":", 1)[-1] if value else "?"


def _list_forwards(wt_id: str):
    """Print all active port forwards for a container."""
    for name in _forward_devices(wt_id):
        print(f"{_device_port(wt_id, name, 'listen')}:{_device_port(wt_id, name, 'connect')}")


@click.command(context_settings={"allow_extra_args": True})
@sandbox_options()
@click.option("--clear", is_flag=True, help="Remove all existing port forwards before adding new ones.")
@click.option("--list", "list_forwards", is_flag=True, help="List active port forwards.")
@click.pass_context
def port_forward_cmd(ctx, match, interactive, clear, list_forwards):
    """Forward ports from the host to a sandbox."""
    sandbox = resolve_sandbox(match=match, interactive=interactive, create="deny")

    # Ensure sandbox is running
    lines = (
        run_in_vm(
            ["incus", "list", "--format=csv", "--columns=ns", sandbox.wt_id],
            "Checking sandbox",
            check=False,
        )
        .stdout.decode()
        .strip()
    )
    if sandbox.wt_id not in lines:
        fail("Did not match an existing sandbox.")
    if "RUNNING" not in lines:
        fail(f"Sandbox is not running. Run {click.style(f'locki x -m {sandbox.wt_id} true', fg='green')} to start it.")

    if clear:
        for name in _forward_devices(sandbox.wt_id):
            run_in_vm(
                ["incus", "config", "device", "remove", sandbox.wt_id, name],
                f"Removing {name}",
            )
        if not ctx.args and not list_forwards:
            return

    for spec in ctx.args:
        host_port, sandbox_port = _parse_port_spec(spec)
        if host_port < 1024:
            fail(f"Host port {host_port} is not allowed (must be >= 1024).")
        run_in_vm(
            [
                "incus",
                "config",
                "device",
                "add",
                sandbox.wt_id,
                f"port-fwd-{host_port}",
                "proxy",
                f"listen=tcp:0.0.0.0:{host_port}",
                f"connect=tcp:127.0.0.1:{sandbox_port}",
            ],
            f"Forwarding host port {host_port} -> sandbox port {sandbox_port}",
        )

    if list_forwards:
        _list_forwards(sandbox.wt_id)
    elif not ctx.args and not clear:
        fail(
            "No ports specified. Usage: locki port-forward [-m <sandbox-name-part>] [--list] [--clear] [port[:port]] ..."
        )
