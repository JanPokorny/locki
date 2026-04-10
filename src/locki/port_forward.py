import logging
import socket
import sys

import click

import locki

logger = logging.getLogger(__name__)


def _free_port() -> int:
    """Find a random free port on the host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _parse_port_spec(spec: str) -> tuple[int, int]:
    """Parse port spec into (host_port, container_port). Host port 0 means random."""
    parts = spec.split(":")
    if len(parts) == 1:
        port = int(parts[0])
        return port, port
    if len(parts) == 2:
        host = _free_port() if parts[0] == "" else int(parts[0])
        return host, int(parts[1])
    raise click.BadParameter(f"Invalid port spec '{spec}'. Use 'port', 'host_port:container_port', or ':container_port'.")


def _looks_like_port(arg: str) -> bool:
    """Return True if arg looks like a port spec rather than a branch name."""
    return arg.startswith(":") or arg.split(":")[0].isdigit()


@click.command(context_settings={"allow_extra_args": True})
@click.option("--clear", is_flag=True, help="Remove all existing port forwards before adding new ones.")
@click.pass_context
def port_forward_cmd(ctx, clear):
    """Forward ports from the host to a branch's container."""
    # Parse raw args: [branch] [port_spec]...
    # First arg is a branch name unless it looks like a port spec.
    args = ctx.args
    branch = None
    port_specs: list[str] = []
    for i, arg in enumerate(args):
        if i == 0 and not _looks_like_port(arg):
            branch = arg
        else:
            port_specs.append(arg)

    if branch:
        wt_path = locki.find_worktree_for_branch(branch)
        if wt_path is None:
            logger.error("No worktree found for branch '%s'.", branch)
            sys.exit(1)
    else:
        wt_path = locki.current_worktree()
        if wt_path is None:
            logger.error("No branch specified and not inside a locki worktree.")
            sys.exit(1)
    wt_id = wt_path.relative_to(locki.WORKTREES_HOME).parts[0]

    # Ensure container is running
    result = locki.run_in_vm(
        ["incus", "list", "--format=csv", "--columns=ns", wt_id],
        "Checking container",
        check=False,
    )
    lines = result.stdout.decode().strip()
    if wt_id not in lines:
        logger.error("Container for branch not found. Run 'locki shell' first to create it.")
        sys.exit(1)
    if "RUNNING" not in lines:
        logger.error("Container is not running. Run 'locki shell' first to start it.")
        sys.exit(1)

    if clear:
        # Remove all existing port-forward devices
        result = locki.run_in_vm(
            ["incus", "config", "device", "list", wt_id],
            "Listing devices",
        )
        for line in result.stdout.decode().splitlines():
            name = line.strip()
            if name.startswith("port-fwd-"):
                locki.run_in_vm(
                    ["incus", "config", "device", "remove", wt_id, name],
                    f"Removing {name}",
                )
        if not port_specs:
            return

    if not port_specs:
        logger.error("No ports specified. Usage: locki port-forward [branch] port[:port]...")
        sys.exit(1)

    for spec in port_specs:
        host_port, container_port = _parse_port_spec(spec)
        if host_port < 1024:
            logger.error("Host port %d is not allowed (must be >= 1024).", host_port)
            sys.exit(1)
        device_name = f"port-fwd-{host_port}"
        locki.run_in_vm(
            [
                "incus",
                "config",
                "device",
                "add",
                wt_id,
                device_name,
                "proxy",
                f"listen=tcp:0.0.0.0:{host_port}",
                f"connect=tcp:127.0.0.1:{container_port}",
            ],
            f"Forwarding host port {host_port} -> container port {container_port}",
        )
        print(f"{host_port}:{container_port}")
