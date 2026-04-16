import sys

import click
import tomlkit

from locki.config import LOCKI_HOME, WORKTREES_HOME
from locki.shell import exec_cmd
from locki.utils import find_worktree_for_branch, git_root, list_locki_worktree_branches, match_sandbox_branch

HARNESSES = ["claude", "gemini", "codex", "opencode"]
RESUME_ARGS = {"claude": ["-c"], "gemini": ["-r"], "codex": ["resume"]}
CONFIG_PATH = LOCKI_HOME / "config.toml"


def _load_harness() -> str | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        data = tomlkit.loads(CONFIG_PATH.read_text())
        harness = data.get("ai", {}).get("harness")
        if harness in HARNESSES:
            return harness
    except Exception:
        pass
    return None


def _save_harness(harness: str) -> None:
    LOCKI_HOME.mkdir(parents=True, exist_ok=True)
    data = tomlkit.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else tomlkit.document()
    if "ai" not in data:
        data.add("ai", tomlkit.table())
    data["ai"]["harness"] = harness
    CONFIG_PATH.write_text(tomlkit.dumps(data))


def _ask_harness() -> str:
    if not sys.stdin.isatty():
        click.echo(
            f"{click.style('ᛞ', fg='red', bold=True)} No default AI harness configured. "
            "Run `locki ai` interactively first to pick one.",
            err=True,
        )
        sys.exit(1)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    selected = inquirer.select(
        message="Select your default AI harness:",
        choices=[Choice(value=h, name=h) for h in HARNESSES],
    ).execute()

    _save_harness(selected)
    click.echo(
        f"{click.style('ᛝ', fg='green', bold=True)} Saved default harness "
        f"{click.style(selected, fg='green')} to {CONFIG_PATH}",
        err=True,
    )
    return selected


@click.command("ai", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@click.option("-b", "--branch", default=None, help="Substring match on existing sandbox branch.")
@click.option("-n", "--new", "new", is_flag=True, default=False, help="Create a new sandbox.")
@click.pass_context
def ai_cmd(ctx, branch, new):
    """Start an AI harness in a sandbox (wrapper around locki x).

    \b
    Examples:
      locki ai                        # pick sandbox, run default harness
      locki ai -b feat                # resume in existing sandbox
      locki ai -n                     # new sandbox, fresh conversation
    """
    harness = _load_harness()
    if harness is None:
        harness = _ask_harness()

    # Determine sandbox and whether we're resuming
    is_new = new
    if not branch and not new:
        wt_branches = list_locki_worktree_branches()
        if not wt_branches:
            is_new = True
        elif sys.stdin.isatty():
            from InquirerPy import inquirer
            from InquirerPy.base.control import Choice

            choices = [Choice(value=None, name="(create new)")] + [
                Choice(value=b, name=b) for b in sorted(wt_branches)
            ]
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
                f"{click.style('ᛞ', fg='red', bold=True)} No branch specified. "
                "Use -b <branch> in non-interactive mode.",
                err=True,
            )
            sys.exit(1)

    git_root()  # fail fast if not in a git repo

    # Build the command args that exec_cmd will run inside the container
    ctx.args = [harness]
    if not is_new and branch:
        if harness == "claude":
            resolved = match_sandbox_branch(branch)
            branch = resolved
            wt_path = find_worktree_for_branch(branch)
            if wt_path:
                wt_id = wt_path.relative_to(WORKTREES_HOME).parts[0]
                projects_dir = LOCKI_HOME / "home" / ".claude" / "projects"
                if projects_dir.is_dir() and any(
                    d.name.endswith(wt_id) for d in projects_dir.iterdir() if d.is_dir()
                ):
                    ctx.args.extend(RESUME_ARGS["claude"])
        else:
            ctx.args.extend(RESUME_ARGS.get(harness, []))

    ctx.invoke(exec_cmd.callback, branch=branch, new=is_new)
