import sys

import click

from locki.cmd.exec import exec_cmd
from locki.config import load_config, save_user_config
from locki.paths import DATA, USER_CONFIG
from locki.runes import ERROR, SUCCESS
from locki.utils import cwd_git_repo, resolve_sandbox

HARNESSES = ["claude", "gemini", "codex", "opencode", "pi"]
RESUME_ARGS = {"claude": ["-c"], "gemini": ["-r"], "codex": ["resume"], "pi": ["-c"]}


def _ask_harness() -> str:
    if not sys.stdin.isatty():
        click.echo(
            f"{ERROR} No default AI harness configured. "
            f"Run {click.style('locki ai', fg='green')} interactively first to pick one, "
            f"or configure e.g. {click.style('ai.harness = "claude"', fg='yellow')} in {click.style(str(USER_CONFIG), fg='cyan')}.",
            err=True,
        )
        sys.exit(1)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    selected = inquirer.select(
        message="Select your default AI harness:",
        choices=[Choice(value=h, name=h) for h in HARNESSES],
    ).execute()

    save_user_config("ai", "harness", selected)
    click.echo(
        f"{SUCCESS} Saved default harness "
        f"{click.style(selected, fg='green')} to {USER_CONFIG}",
        err=True,
    )
    return selected


@click.command("ai", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@click.option("-m", "--match", default=None, help="Substring match on existing sandbox branch.")
@click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")
@click.option("-a", "--all", "all_repos", is_flag=True, default=False, help="Show sandboxes from all repos.")
@click.option("-c", "--create", is_flag=True, default=False, help="Create a new sandbox.")
@click.option("-f", "--id-file", default=None, type=click.Path(), help="Write the generated sandbox ID to this file.")
@click.pass_context
def ai_cmd(ctx, match, interactive, all_repos, create, id_file):
    """Start an AI harness in a sandbox (wrapper around locki x).

    \b
    Examples:
      locki ai                        # current sandbox / picker / create
      locki ai -m feat                # resume in existing sandbox
      locki ai -i                     # force sandbox picker
      locki ai -a                     # picker across all repos
      locki ai -c                     # new sandbox, fresh conversation
    """
    if create and (match or interactive or all_repos):
        click.echo(
            f"{ERROR} --create conflicts with --match/--interactive/--all.",
            err=True,
        )
        sys.exit(1)

    config = load_config(cwd_git_repo())
    harness = config.ai.harness if config.ai.harness in HARNESSES else None
    if harness is None:
        harness = _ask_harness()

    sandbox = None
    if not create:
        sandbox = resolve_sandbox(
            match=match,
            interactive=interactive,
            all_repos=all_repos,
            allow_create=True,
        )

    ctx.args = [harness]

    if sandbox is not None:
        # Resuming an existing sandbox; infer resume flag for the harness.
        wt_id = sandbox.wt_id
        if harness == "claude":
            projects_dir = DATA / "home" / ".claude" / "projects"
            if projects_dir.is_dir() and any(d.name.endswith(wt_id) for d in projects_dir.iterdir() if d.is_dir()):
                ctx.args.extend(RESUME_ARGS["claude"])
        else:
            ctx.args.extend(RESUME_ARGS.get(harness, []))
        match_arg = sandbox.wt_id
        is_new = False
    else:
        match_arg = None
        is_new = True

    ctx.invoke(
        exec_cmd.callback,
        match=match_arg,
        interactive=False,
        all_repos=False,
        create=is_new,
        id_file=id_file,
    )
