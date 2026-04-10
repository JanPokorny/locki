import click

from locki import AliasGroup
from locki.utils import run_command


@click.group(cls=AliasGroup, help="Manage the Locki VM.")
def vm_app():
    pass


@vm_app.command("stop", help="Stop the Locki VM.")
def vm_stop_cmd():
    import locki
    run_command(
        [locki.limactl(), "stop", "locki"],
        "Stopping VM",
        env={"LIMA_HOME": str(locki.LIMA_HOME)},
        cwd="/",
    )


@vm_app.command("delete | remove | rm", help="Delete the Locki VM entirely.")
def vm_delete_cmd():
    import locki
    run_command(
        [locki.limactl(), "delete", "-f", "locki"],
        "Deleting VM",
        env={"LIMA_HOME": str(locki.LIMA_HOME)},
        cwd="/",
    )
