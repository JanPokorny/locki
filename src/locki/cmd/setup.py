import contextlib
import os
import pathlib
import shutil
import sys

import click

from locki.cmd.ai import HARNESSES
from locki.cmd.ide import EDITORS
from locki.config import save_user_config
from locki.paths import DATA, HOME, USER_CONFIG
from locki.runes import INFO, SUCCESS

COPY_DIRS = [
    ".claude",
    ".gemini",
    ".codex",
    ".pi",
    ".config/opencode",
    ".config/github-copilot",
    ".config/gh-copilot",
    ".local/share/opencode",
]
COPY_FILES = [
    ".claude.json",
]
SANDBOX_HOME = DATA / "home"


@click.group("config")
def config_app():
    """Manage Locki configuration."""


@config_app.command("setup")
@click.option("--defaults", is_flag=True, default=False, help="Skip interactive prompts, use defaults.")
def setup_cmd(defaults: bool):
    """Interactive setup wizard (AI harness, editor, config copy)."""
    if defaults or not sys.stdin.isatty():
        if not USER_CONFIG.exists():
            save_user_config("ai", "harness", "claude")
            save_user_config("ide", "editor", "code")
        return

    click.echo(click.style("\nWelcome to Locki! Let's set up.\n", fg="yellow"), err=True)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    harness = inquirer.select(
        message="Default AI harness for 'locki ai':",
        choices=[Choice(value=h, name=h) for h in HARNESSES],
    ).execute()
    save_user_config("ai", "harness", harness)

    available_editors = [(cmd, label) for cmd, label in EDITORS if shutil.which(cmd)]
    if available_editors:
        editor = inquirer.select(
            message="Default editor for 'locki ide':",
            choices=[Choice(value=cmd, name=label) for cmd, label in available_editors],
        ).execute()
        save_user_config("ide", "editor", editor)
    else:
        click.echo(f"{INFO} No supported editors found in PATH, skipping.", err=True)

    sources = [HOME / p for p in COPY_DIRS + COPY_FILES if (HOME / p).exists()]
    if sources:
        click.echo(f"\n{INFO} Found AI config files/folders that can be copied into the sandbox:", err=True)
        for p in sources:
            click.echo(f"     ~/{p.relative_to(HOME)}", err=True)
        if inquirer.confirm(message="Copy these into the sandbox home?", default=True).execute():
            copy_cmd.callback()  # pyrefly: ignore[not-callable]
        else:
            click.echo(f"{INFO} Skipped. You can copy later with {click.style('locki config copy', fg='green')}.", err=True)

    click.echo(f"\n{SUCCESS} Config saved to {USER_CONFIG}", err=True)
    click.echo(f"{SUCCESS} Re-run setup anytime with {click.style('locki config setup', fg='green')}.", err=True)


@config_app.command("copy")
def copy_cmd():
    """Copy AI config files from ~ into the sandbox home."""
    for rel in COPY_DIRS:
        src = HOME / rel
        if not src.exists():
            continue
        dst = SANDBOX_HOME / rel
        for dirpath, _dirnames, filenames in os.walk(src):
            rel_dir = pathlib.Path(dirpath).relative_to(src)
            (dst / rel_dir).mkdir(parents=True, exist_ok=True)
            for name in filenames:
                s = pathlib.Path(dirpath) / name
                d = dst / rel_dir / name
                with contextlib.suppress(OSError):
                    if d.exists() or d.is_symlink():
                        d.unlink()
                    if s.is_symlink():
                        os.symlink(os.readlink(s), d)
                    else:
                        shutil.copy2(s, d)
    for rel in COPY_FILES:
        src = HOME / rel
        if src.exists():
            dst = SANDBOX_HOME / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    click.echo(f"{SUCCESS} Copied AI config files to sandbox home.", err=True)
