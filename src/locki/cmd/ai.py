import shlex

import click

from locki.cmd.exec import enter_sandbox
from locki.cmd.setup import AGENT_TEMPLATES, ensure_configured
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
    """Start an agent in a sandbox (wrapper around locki x).

    \b
    Examples:
      locki ai                        # current sandbox / picker / create
      locki ai codex                  # a specific agent (claude, codex, opencode, agy, pi, copilot)
      locki ai -m feat                # resume in existing sandbox
      locki ai -m feat claude -p 'x'  # agent name first, then its args
      locki ai -i                     # force sandbox picker
      locki ai -n                     # new sandbox, fresh conversation
      locki ai -p 'fix the tests'     # extra args go to the agent
    """

    worktree = worktrees.resolve(
        match=match,
        interactive=interactive,
        create="force" if create else "allow",
    )

    config_command = shlex.split(ensure_configured(worktree.repo).ai_command)
    agents = {name.lower(): shlex.split(cmd) for name, cmd in AGENT_TEMPLATES.items()}
    agents |= {cmd[0]: cmd for cmd in agents.values()}  # executable names too: `locki ai agy`
    agent, args = (
        (ctx.args[0].lower(), ctx.args[1:]) if ctx.args and ctx.args[0].lower() in agents else (None, ctx.args)
    )
    # the configured line wins over the template when it is the same agent (keeps the user's own flags)
    ai_command = config_command if agent is None or agents[agent][0] == config_command[0] else agents[agent]

    if ai_command[0] == "claude" and not home.has_claude_transcript(worktree.path):
        ai_command = [a for a in ai_command if a not in ("-c", "--continue")]

    enter_sandbox(worktree, [*ai_command, *args], agent=agent)
