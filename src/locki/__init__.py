import click

from locki.cmd.ai import ai_cmd
from locki.cmd.cd import cd_cmd
from locki.cmd.exec import exec_cmd
from locki.cmd.ide import ide_cmd
from locki.cmd.include import include_cmd
from locki.cmd.internal import internal_app
from locki.cmd.list import list_cmd
from locki.cmd.new import new_cmd
from locki.cmd.port_forward import port_forward_cmd
from locki.cmd.remove import remove_cmd
from locki.cmd.setup import setup_cmd
from locki.cmd.stop import stop_cmd
from locki.cmd.vm import vm_app
from locki.logging import setup_logging
from locki.utils import AliasGroup

setup_logging()


@click.group(
    cls=AliasGroup,
    help="AI sandboxing without the taste of sand, using a managed Lima VM with Incus containers.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(None, "-v", "--version", package_name="locki", prog_name="locki")
def app():
    pass


app.add_command(ai_cmd, "ai")
app.add_command(cd_cmd, "cd")
app.add_command(exec_cmd, "exec | x")
app.add_command(ide_cmd, "ide")
app.add_command(include_cmd, "include")
app.add_command(internal_app, "internal")
app.add_command(new_cmd, "new | n")
app.add_command(list_cmd, "list | ls")
app.add_command(port_forward_cmd, "port-forward | pf")
app.add_command(remove_cmd, "remove | rm | delete")
app.add_command(setup_cmd, "setup")
app.add_command(stop_cmd, "stop")
app.add_command(vm_app, "vm")
