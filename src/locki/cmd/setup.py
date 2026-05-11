import contextlib
import os
import pathlib
import shutil
import sys

import click

from locki.config import save_user_config
from locki.paths import HOME, SANDBOX_HOME, USER_CONFIG
from locki.runes import INFO, SUCCESS

AI_TEMPLATES = {
    "Claude": "claude --dangerously-skip-permissions -c",
    "Gemini": "gemini --yolo -r",
    "Codex": "codex --yolo resume",
    "OpenCode": "opencode",
    "Pi": "pi -c",
    "Copilot": "copilot --yolo --no-auto-update --continue",
}

IDE_TEMPLATES = {
    "VSCode": "code .",
    "Zed": "zed .",
    "Fresh": "fresh .",
}

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


@click.command("setup")
@click.option("--defaults", is_flag=True, default=False, help="Skip interactive prompts, use defaults.")
@click.option("--copy", "copy_only", is_flag=True, default=False, help="Only copy AI config files into sandbox home.")
def setup_cmd(defaults: bool, copy_only: bool):
    """Interactive setup wizard (AI harness, editor, config copy)."""
    do_copy = copy_only

    if not copy_only:
        if defaults or not sys.stdin.isatty():
            if not USER_CONFIG.exists():
                save_user_config("ai_command", AI_TEMPLATES["Claude"])
                save_user_config("ide_command", IDE_TEMPLATES["VSCode"])
            return

        click.echo(click.style("\nWelcome to Locki! Let's set up.\n", fg="yellow"), err=True)

        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice

        ai_command = inquirer.select(
            message="Default AI harness for 'locki ai':",
            choices=[Choice(value=cmd, name=name) for name, cmd in AI_TEMPLATES.items()],
        ).execute()
        save_user_config("ai_command", ai_command)

        available = {name: cmd for name, cmd in IDE_TEMPLATES.items() if shutil.which(cmd.split()[0])}
        if available:
            ide_command = inquirer.select(
                message="Default editor for 'locki ide':",
                choices=[Choice(value=cmd, name=name) for name, cmd in available.items()],
            ).execute()
            save_user_config("ide_command", ide_command)
        else:
            ide_bins = sorted({cmd.split()[0] for cmd in IDE_TEMPLATES.values()})
            click.echo(f"{INFO} No supported editors found in PATH ({', '.join(ide_bins)}), skipping.", err=True)

        sources = [HOME / p for p in COPY_DIRS + COPY_FILES if (HOME / p).exists()]
        if sources:
            click.echo(f"\n{INFO} Found AI config files/folders that can be copied into the sandbox:", err=True)
            for p in sources:
                click.echo(f"     ~/{p.relative_to(HOME)}", err=True)
            do_copy = inquirer.confirm(message="Copy these into the sandbox home?", default=True).execute()
            if not do_copy:
                click.echo(
                    f"{INFO} Skipped. You can copy later with {click.style('locki setup --copy', fg='green')}.",
                    err=True,
                )

    if do_copy:
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
        click.echo(
            f"{SUCCESS} Copied AI config files to sandbox home. Copy again anytime with {click.style('locki setup --copy', fg='green')}.",
            err=True,
        )

    if not copy_only:
        click.echo(f"\n{SUCCESS} Config saved to {USER_CONFIG}", err=True)
        click.echo(f"{SUCCESS} Re-run setup anytime with {click.style('locki setup', fg='green')}.", err=True)
