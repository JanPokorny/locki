import os
import sys

import click

from locki.config import LOCKI_HOME
from locki.utils import git_root, list_locki_worktree_branches

HARNESSES = ["claude", "gemini", "codex", "opencode"]
RESUME_ARGS = {"claude": ["-c"], "gemini": ["-r"], "codex": ["resume"]}
CONFIG_PATH = LOCKI_HOME / "config.toml"


def _load_harness() -> str | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        import tomllib

        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        harness = data.get("ai", {}).get("harness")
        if harness in HARNESSES:
            return harness
    except Exception:
        pass
    return None


def _save_harness(harness: str) -> None:
    LOCKI_HOME.mkdir(parents=True, exist_ok=True)
    import tomllib

    data: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
    data.setdefault("ai", {})["harness"] = harness

    parts: list[str] = []
    for section, values in data.items():
        if isinstance(values, dict):
            parts.append(f"[{section}]")
            for k, v in values.items():
                parts.append(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}")
            parts.append("")
    CONFIG_PATH.write_text("\n".join(parts))


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

    # Build locki x arguments
    args = ["locki", "x"]
    if is_new:
        args.append("-n")
    elif branch:
        args.extend(["-b", branch])

    args.append(harness)

    # Add resume args when returning to an existing sandbox
    if not is_new and branch:
        args.extend(RESUME_ARGS.get(harness, []))

    # Pass through any extra args from the user
    args.extend(ctx.args)

    locki_bin = _find_locki_bin()
    os.execvp(locki_bin[0], locki_bin + args[1:])


def _find_locki_bin() -> list[str]:
    import shutil

    locki_path = shutil.which("locki")
    if locki_path:
        return [locki_path]
    return [sys.executable, "-m", "locki"]
