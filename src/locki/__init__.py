import os
import sys
from importlib.metadata import version

import click

from locki.cmd.ai import ai_cmd
from locki.cmd.exec import exec_cmd
from locki.cmd.ide import ide_cmd
from locki.cmd.include import include_cmd
from locki.cmd.internal import internal_app
from locki.cmd.list import list_cmd
from locki.cmd.new import new_cmd
from locki.cmd.port_forward import port_forward_cmd
from locki.cmd.remove import remove_cmd
from locki.cmd.setup import config_app
from locki.cmd.vm import vm_app
from locki.logging import setup_logging
from locki.paths import LIMA, USER_CONFIG
from locki.utils import AliasGroup

os.environ["LIMA_HOME"] = str(LIMA)  # limactl reads this; set early so every subprocess inherits it

setup_logging()


@click.group(
    cls=AliasGroup,
    help="AI sandboxing without the taste of sand, using a managed Lima VM with Incus containers.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version("locki"), "-v", "--version", prog_name="locki")
@click.pass_context
def app(ctx):
    if not USER_CONFIG.exists() and ctx.invoked_subcommand not in ("config", "internal", None):
        from locki.cmd.setup import setup_cmd
        ctx.invoke(setup_cmd, defaults=not sys.stdin.isatty())

app.add_command(ai_cmd, "ai")
app.add_command(exec_cmd, "exec | x")
app.add_command(ide_cmd, "ide")
app.add_command(include_cmd, "include")
app.add_command(internal_app, "internal")
app.add_command(new_cmd, "new | n")
app.add_command(list_cmd, "list | ls")
app.add_command(port_forward_cmd, "port-forward | pf")
app.add_command(remove_cmd, "remove | rm | delete")
app.add_command(config_app, "config")
app.add_command(vm_app, "vm")
