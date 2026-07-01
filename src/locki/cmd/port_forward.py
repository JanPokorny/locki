import json
import socket

import click

from locki.utils import fail, json_option, resolve_sandbox, run_in_vm, sandbox_options


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


def _port(address: str) -> int | str:
    """Last `:`-separated field of a proxy device address, e.g. tcp:0.0.0.0:8080 -> 8080."""
    value = address.rsplit(":", 1)[-1]
    return int(value) if value.isdigit() else value or "?"


@click.command(context_settings={"allow_extra_args": True})
@sandbox_options()
@click.option("--clear", is_flag=True, help="Remove all existing port forwards before adding new ones.")
@click.option("--list", "list_forwards", is_flag=True, help="List active port forwards.")
@json_option
@click.pass_context
def port_forward_cmd(ctx, match, interactive, clear, list_forwards, as_json):
    """Forward ports from the host to a sandbox."""
    sandbox = resolve_sandbox(match=match, interactive=interactive, create="deny")

    # Fetch the container's status and devices in a single `incus list` roundtrip.
    result = run_in_vm(
        ["incus", "list", "--format=json", sandbox.wt_id],
        "Checking sandbox",
        check=False,
    )
    try:
        containers = json.loads(result.stdout.decode())
    except json.JSONDecodeError:
        containers = []
    container = next((c for c in containers if c.get("name") == sandbox.wt_id), None)
    if container is None:
        fail("Did not match an existing sandbox.")
    if container.get("status", "").lower() != "running":
        fail(f"Sandbox is not running. Run {click.style(f'locki x -m {sandbox.wt_id} true', fg='green')} to start it.")

    # Proxy devices on the container, tracked in memory as we mutate them.
    devices = {name: dev for name, dev in (container.get("devices") or {}).items() if name.startswith("port-fwd-")}

    if clear:
        for name in devices:
            run_in_vm(
                ["incus", "config", "device", "remove", sandbox.wt_id, name],
                f"Removing {name}",
            )
        devices = {}
        if not ctx.args and not list_forwards:
            if as_json:
                click.echo(json.dumps([]))
            return

    added = []
    for spec in ctx.args:
        host_port, sandbox_port = _parse_port_spec(spec)
        if host_port < 1024:
            fail(f"Host port {host_port} is not allowed (must be >= 1024).")
        name = f"port-fwd-{host_port}"
        if name in devices:
            run_in_vm(
                ["incus", "config", "device", "remove", sandbox.wt_id, name],
                f"Removing existing forward on host port {host_port}",
                check=False,
                quiet=True,
            )
        run_in_vm(
            [
                "incus",
                "config",
                "device",
                "add",
                sandbox.wt_id,
                name,
                "proxy",
                f"listen=tcp:0.0.0.0:{host_port}",
                f"connect=tcp:127.0.0.1:{sandbox_port}",
            ],
            f"Forwarding host port {host_port} -> sandbox port {sandbox_port}",
        )
        devices[name] = {"listen": f"tcp:0.0.0.0:{host_port}", "connect": f"tcp:127.0.0.1:{sandbox_port}"}
        added.append({"host_port": host_port, "sandbox_port": sandbox_port})

    if list_forwards:
        forwards = [
            {"host_port": _port(dev.get("listen", "")), "sandbox_port": _port(dev.get("connect", ""))}
            for dev in devices.values()
        ]
        if as_json:
            click.echo(json.dumps(forwards))
        else:
            for f in forwards:
                print(f"{f['host_port']}:{f['sandbox_port']}")
    elif ctx.args:
        if as_json:
            click.echo(json.dumps(added))
    elif not clear:
        fail(
            "No ports specified. Usage: locki port-forward [-m <sandbox-name-part>] [--list] [--clear] [port[:port]] ..."
        )
