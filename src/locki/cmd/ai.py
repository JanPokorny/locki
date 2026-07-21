import re
import shlex
import uuid

import click

from locki.cmd.exec import exec_cmd
from locki.config import load_config
from locki.paths import SANDBOX_HOME
from locki.services.worktree import worktrees
from locki.utils import sandbox_options


@click.command("ai", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@sandbox_options(create=True)
@click.pass_context
def ai_cmd(ctx, match, interactive, create):
    """Start an AI harness in a sandbox (wrapper around locki x).

    \b
    Examples:
      locki ai                        # current sandbox / picker / create
      locki ai -m feat                # resume in existing sandbox
      locki ai -i                     # force sandbox picker
      locki ai -n                     # new sandbox, fresh conversation
    """

    ai_command = load_config(worktrees.cwd_repo).ai_command

    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    if shlex.split(ai_command)[0] == "claude":
        project_name = re.sub(r"[^a-zA-Z0-9]", "-", str(worktree.wt_path))
        project_dir = SANDBOX_HOME / ".claude" / "projects" / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        if not any(project_dir.glob("*.jsonl")):
            (project_dir / f"{uuid.uuid4()}.jsonl").write_text("\n")

    ctx.args = shlex.split(ai_command)
    ctx.obj = worktree
    ctx.invoke(exec_cmd.callback, match=None, interactive=False, create=False)
