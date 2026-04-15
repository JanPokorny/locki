import click

from locki.logging import setup_logging
from locki.port_forward import port_forward_cmd
from locki.self_service import self_service_cmd
from locki.shell import exec_cmd
from locki.utils import AliasGroup
from locki.vm import vm_app
from locki.worktree import list_cmd, remove_cmd, stop_cmd

setup_logging()
app = click.group(cls=AliasGroup, help="AI sandboxing without the taste of sand, using a managed Lima VM with Incus containers.")(lambda: None)
app.add_command(exec_cmd, "exec | x")
app.add_command(port_forward_cmd, "port-forward | pf")
app.add_command(remove_cmd, "remove | rm | delete")
app.add_command(stop_cmd, "stop")
app.add_command(list_cmd, "list | ls")
app.add_command(self_service_cmd, "self-service")
app.add_command(vm_app, "vm")
