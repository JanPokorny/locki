import shlex
import uuid

import click

from locki.cmd.exec import enter_sandbox
from locki.cmd.setup import ensure_configured
from locki.services.home import home
from locki.services.worktree import worktrees
from locki.utils import sandbox_options


@click.command(
    "ai",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "allow_interspersed_args": False},
)
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
      locki ai -p 'fix the tests'     # extra args go to the AI command
    """

    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    ai_command = ensure_configured(worktree.repo).ai_command

    if shlex.split(ai_command)[0] == "claude":
        # claude -c needs an existing transcript; plant an empty one for fresh sandboxes
        project_dir = home.claude_project_dir(worktree.wt_path)
        project_dir.mkdir(parents=True, exist_ok=True)
        if not any(project_dir.glob("*.jsonl")):
            (project_dir / f"{uuid.uuid4()}.jsonl").write_text("\n")

    enter_sandbox(worktree, [*shlex.split(ai_command), *ctx.args])
