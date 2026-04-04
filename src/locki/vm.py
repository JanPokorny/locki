import pathlib

from locki.async_typer import AsyncTyper
from locki.utils import run_command

vm_app = AsyncTyper(name="vm", help="Manage the Locki VM.", no_args_is_help=True)


@vm_app.command("stop", help="Stop the Locki VM.")
async def vm_stop_cmd():
    import locki
    await run_command(
        [locki.limactl(), "stop", "locki"],
        "Stopping VM",
        env={"LIMA_HOME": str(locki.LIMA_HOME)},
        cwd="/",
    )


@vm_app.command("delete | remove | rm", help="Delete the Locki VM entirely.")
async def vm_delete_cmd():
    import locki
    await run_command(
        [locki.limactl(), "delete", "-f", "locki"],
        "Deleting VM",
        env={"LIMA_HOME": str(locki.LIMA_HOME)},
        cwd="/",
    )
