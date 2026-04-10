import logging
import sys
import typing

import typer

import locki

logger = logging.getLogger(__name__)


def _parse_port_spec(spec: str) -> tuple[int, int]:
    """Parse 'host_port:container_port' or 'port' into (host_port, container_port)."""
    parts = spec.split(":")
    if len(parts) == 1:
        port = int(parts[0])
        return port, port
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    raise typer.BadParameter(f"Invalid port spec '{spec}'. Use 'port' or 'host_port:container_port'.")


async def port_forward_cmd(
    branch: typing.Annotated[
        str | None, typer.Argument(help="Branch name (optional if inside a worktree)")
    ] = None,
    clear: typing.Annotated[
        bool, typer.Option("--clear", help="Remove all existing port forwards before adding new ones")
    ] = False,
    ports: typing.Annotated[
        list[str] | None, typer.Argument(help="Ports to forward: 'port' or 'host_port:container_port'")
    ] = None,
):
    """Forward ports from the host to a branch's container."""
    if branch:
        wt_path = await locki.find_worktree_for_branch(branch)
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
    result = await locki.run_in_vm(
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
        result = await locki.run_in_vm(
            ["incus", "config", "device", "list", wt_id],
            "Listing devices",
        )
        for line in result.stdout.decode().splitlines():
            name = line.strip()
            if name.startswith("port-fwd-"):
                await locki.run_in_vm(
                    ["incus", "config", "device", "remove", wt_id, name],
                    f"Removing {name}",
                )
        if not ports:
            return

    if not ports:
        logger.error("No ports specified. Usage: locki port-forward [branch] port[:port]...")
        sys.exit(1)

    for spec in ports:
        host_port, container_port = _parse_port_spec(spec)
        if host_port < 1024:
            logger.error("Host port %d is not allowed (must be >= 1024).", host_port)
            sys.exit(1)
        device_name = f"port-fwd-{host_port}"
        await locki.run_in_vm(
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
