import shlex

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
        home.ensure_resume_transcript(worktree.wt_path)

    enter_sandbox(worktree, [*shlex.split(ai_command), *ctx.args])
