from locki.async_typer import AsyncTyper
from locki.utils import run_command

vm_app = AsyncTyper(name="vm", help="Manage the Locki VM.", no_args_is_help=True)


def _lima_env():
    import pathlib
    return {"LIMA_HOME": str(pathlib.Path.home() / ".locki" / "lima")}


@vm_app.command("stop", help="Stop the Locki VM.")
async def vm_stop_cmd():
    from locki import limactl
    await run_command(
        [limactl(), "stop", "locki"],
        "Stopping VM",
        env=_lima_env(),
        cwd="/",
    )


@vm_app.command("delete | remove | rm", help="Delete the Locki VM entirely.")
async def vm_delete_cmd():
    from locki import limactl
    await run_command(
        [limactl(), "delete", "-f", "locki"],
        "Deleting VM",
        env=_lima_env(),
        cwd="/",
    )
