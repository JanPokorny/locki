import os

import click

from locki.cmd.ai import ai_cmd
from locki.cmd.exec import exec_cmd
from locki.cmd.list import list_cmd
from locki.cmd.port_forward import port_forward_cmd
from locki.cmd.remove import remove_cmd
from locki.cmd.self_service import self_service_cmd
from locki.cmd.vm import vm_app
from locki.logging import setup_logging
from locki.paths import LIMA
from locki.utils import AliasGroup

os.environ["LIMA_HOME"] = str(LIMA)  # limactl reads this; set early so every subprocess inherits it

setup_logging()
app = click.group(
    cls=AliasGroup, help="AI sandboxing without the taste of sand, using a managed Lima VM with Incus containers."
)(lambda: None)
app.add_command(ai_cmd, "ai")
app.add_command(exec_cmd, "exec | x")
app.add_command(port_forward_cmd, "port-forward | pf")
app.add_command(remove_cmd, "remove | rm | delete")
app.add_command(list_cmd, "list | ls")
app.add_command(self_service_cmd, "self-service")
app.add_command(vm_app, "vm")
