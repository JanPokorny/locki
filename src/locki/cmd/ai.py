import sys

import click

from locki.cmd.exec import exec_cmd
from locki.config import load_config, save_user_config
from locki.paths import DATA, USER_CONFIG, WORKTREES
from locki.utils import (
    current_worktree,
    find_worktree_for_branch,
    git_root,
    list_locki_worktree_branches,
    match_sandbox_branch,
)

HARNESSES = ["claude", "gemini", "codex", "opencode", "pi"]
RESUME_ARGS = {"claude": ["-c"], "gemini": ["-r"], "codex": ["resume"], "pi": ["-c"]}


def _ask_harness() -> str:
    if not sys.stdin.isatty():
        click.echo(
            f"{click.style('ᛞ', fg='red', bold=True)} No default AI harness configured. "
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
        f"{click.style('ᛝ', fg='green', bold=True)} Saved default harness "
        f"{click.style(selected, fg='green')} to {USER_CONFIG}",
        err=True,
    )
    return selected


@click.command("ai", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@click.option(
    "-m",
    "--match",
    "-b",
    "--branch",
    "match",
    default=None,
    help="Substring match on existing sandbox branch or sandbox ID.",
)
@click.option("-s", "--select", is_flag=True, default=False, help="Show interactive sandbox selector.")
@click.option("-c", "--create", is_flag=True, default=False, help="Create a new sandbox.")
@click.option("-f", "--id-file", default=None, type=click.Path(), help="Write the generated sandbox ID to this file.")
@click.pass_context
def ai_cmd(ctx, match, select, create, id_file):
    """Start an AI harness in a sandbox (wrapper around locki x).

    \b
    Examples:
      locki ai                        # pick sandbox, run default harness
      locki ai -m feat                # resume in existing sandbox
      locki ai -s                     # force sandbox selector
      locki ai -c                     # new sandbox, fresh conversation
    """
    if create and match:
        click.echo(
            f"{click.style('ᛞ', fg='red', bold=True)} --create and --match cannot be used together.",
            err=True,
        )
        sys.exit(1)

    config = load_config(git_root())
    harness = config.ai.harness if config.ai.harness in HARNESSES else None
    if harness is None:
        harness = _ask_harness()

    # Determine sandbox and whether we're resuming
    is_new = create
    branch: str | None = None
    if match:
        branch = match
    elif select:
        pass  # fall through to exec_cmd which handles the selector
    elif not create and not current_worktree():
        # In main checkout with no flags — show selector or create new
        wt_branches = list_locki_worktree_branches()
        if not wt_branches:
            is_new = True
        elif sys.stdin.isatty():
            from InquirerPy import inquirer
            from InquirerPy.base.control import Choice

            choices = [Choice(value=None, name="(create new)")] + [Choice(value=b, name=b) for b in sorted(wt_branches)]
            selected = inquirer.fuzzy(
                message="Select a sandbox:",
                choices=choices,
            ).execute()

            if selected is None:
                is_new = True
            else:
                branch = selected
        else:
            click.echo(
                f"{click.style('ᛞ', fg='red', bold=True)} No branch specified. Use -m/-b <query> in non-interactive mode.",
                err=True,
            )
            sys.exit(1)

    # Build the command args that exec_cmd will run inside the container
    ctx.args = [harness]
    if not is_new and not select:
        if branch:
            branch = match_sandbox_branch(branch)
            wt_path = find_worktree_for_branch(branch)
        else:
            wt_path = current_worktree()
        if wt_path:
            if harness == "claude":
                wt_id = wt_path.relative_to(WORKTREES).parts[0]
                projects_dir = DATA / "home" / ".claude" / "projects"
                if projects_dir.is_dir() and any(d.name.endswith(wt_id) for d in projects_dir.iterdir() if d.is_dir()):
                    ctx.args.extend(RESUME_ARGS["claude"])
            else:
                ctx.args.extend(RESUME_ARGS.get(harness, []))

    ctx.invoke(exec_cmd.callback, match=branch, select=select, create=is_new, id_file=id_file)
