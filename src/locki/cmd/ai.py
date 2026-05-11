import json
import re
import shlex
import uuid

import click

from locki.cmd.exec import exec_cmd
from locki.config import load_config
from locki.paths import SANDBOX_HOME
from locki.utils import cwd_git_repo, resolve_sandbox


@click.command("ai", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@click.option("-m", "--match", default=None, help="Substring match on existing sandbox branch.")
@click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("-n", "--new", "create", is_flag=True, default=False, help="Create a new sandbox.")
@click.option("-f", "--id-file", default=None, type=click.Path(), help="Write the generated sandbox ID to this file.")
@click.pass_context
def ai_cmd(ctx, match, interactive, create, id_file):
    """Start an AI harness in a sandbox (wrapper around locki x).

    \b
    Examples:
      locki ai                        # current sandbox / picker / create
      locki ai -m feat                # resume in existing sandbox
      locki ai -i                     # force sandbox picker
      locki ai -n                     # new sandbox, fresh conversation
    """

    ai_command = load_config(cwd_git_repo()).ai_command

    sandbox = resolve_sandbox(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if shlex.split(ai_command)[0] == "claude":
        project_name = re.sub(r"[^a-zA-Z0-9]", "-", str(sandbox.wt_path))
        project_dir = SANDBOX_HOME / ".claude" / "projects" / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        if not any(project_dir.glob("*.jsonl")):
            (project_dir / f"{uuid.uuid4()}.jsonl").write_text("\n")

    ctx.args = shlex.split(ai_command)
    ctx.obj = sandbox
    ctx.invoke(
        exec_cmd.callback,
        match=sandbox.wt_id,
        interactive=False,
        create=False,
        id_file=id_file,
    )
