import contextlib
import os
import pathlib
import shlex
import shutil
import sys
import time

import click

from locki.config import LockiConfig, load_config, save_user_config
from locki.paths import HOME, SANDBOX_HOME, USER_CONFIG
from locki.runes import INFO, SUCCESS

AI_TEMPLATES = {
    "Claude": "claude --dangerously-skip-permissions -c",
    "Antigravity": "agy --dangerously-skip-permissions -c",
    "Codex": "codex --yolo resume",
    "OpenCode": "opencode",
    "Pi": "pi -c",
    "Copilot": "copilot --yolo --no-auto-update --continue",
}

IDE_TEMPLATES = {
    "Android Studio": "studio .",
    "BBEdit": "bbedit .",
    "Cursor": "cursor .",
    "Emacs": "emacs .",
    "Fresh": "fresh .",
    "GNOME Builder": "gnome-builder .",
    "Gram": "gram .",
    "JetBrains CLion": "clion .",
    "JetBrains DataGrip": "datagrip .",
    "JetBrains GoLand": "goland .",
    "JetBrains IntelliJ IDEA": "idea .",
    "JetBrains PhpStorm": "phpstorm .",
    "JetBrains PyCharm": "pycharm .",
    "JetBrains Rider": "rider .",
    "JetBrains RubyMine": "rubymine .",
    "JetBrains RustRover": "rustrover .",
    "JetBrains WebStorm": "webstorm .",
    "KDevelop": "kdevelop .",
    "Kate": "kate .",
    "Lapce": "lapce .",
    "Lite XL": "lite-xl .",
    "Neovide": "neovide .",
    "Neovim": "nvim .",
    "Nova": "nova .",
    "Positron": "positron .",
    "Pulsar": "pulsar .",
    "Sublime Text": "subl .",
    "TextMate": "mate .",
    "Trae": "trae .",
    "VSCode": "code .",
    "VSCodium": "codium .",
    "Windsurf": "windsurf .",
    "Zed": "zed .",
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

        available = {name: cmd for name, cmd in IDE_TEMPLATES.items() if shutil.which(shlex.split(cmd)[0])}
        ide_command = inquirer.select(
            message="Default editor for 'locki ide':",
            choices=[
                *([] if available else [Choice(value="", name="(skip)")]),
                *(Choice(value=cmd, name=name) for name, cmd in available.items()),
                Choice(value=None, name="(enter custom command)"),
            ],
            default=0,
        ).execute()
        if ide_command is None:
            ide_command = inquirer.text(
                message="Command to execute in the worktree folder (e.g. `code .`):",
            ).execute()
        save_user_config("ide_command", ide_command)

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
        backup_suffix = f".{int(time.time())}.backup"

        def copy_with_backup(src: pathlib.Path, dst: pathlib.Path) -> None:
            with contextlib.suppress(OSError):
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() or dst.is_symlink():
                    dst.rename(dst.with_name(dst.name + backup_suffix))
                # Follow symlinks: a symlinked "copy" would let sandbox settings
                # writes escape to the host target (e.g. bypassPermissions merged
                # into the real ~/.claude/settings.json).
                shutil.copy2(src, dst)
                dst.chmod(dst.stat().st_mode | 0o600)  # store-sourced files arrive mode 444

        for rel in COPY_DIRS:
            src_root = HOME / rel
            walked: set[pathlib.Path] = set()
            for dirpath, dirnames, filenames in os.walk(src_root, followlinks=True):
                # following symlinked subdirectories can walk in circles (`x -> .`)
                if (real := pathlib.Path(dirpath).resolve()) in walked:
                    dirnames.clear()
                    continue
                walked.add(real)
                for name in filenames:
                    src = pathlib.Path(dirpath) / name
                    copy_with_backup(src, SANDBOX_HOME / rel / src.relative_to(src_root))
        for rel in COPY_FILES:
            if (HOME / rel).exists():
                copy_with_backup(HOME / rel, SANDBOX_HOME / rel)
        click.echo(
            f"{SUCCESS} Copied AI config files to sandbox home. Copy again anytime with {click.style('locki setup --copy', fg='green')}.",
            err=True,
        )

    if not copy_only:
        click.echo(f"\n{SUCCESS} Config saved to {USER_CONFIG}", err=True)
        click.echo(f"{SUCCESS} Re-run setup anytime with {click.style('locki setup', fg='green')}.", err=True)


def ensure_configured(git_root: pathlib.Path | None) -> LockiConfig:
    """Load config, running the setup wizard first if ai/ide commands are missing."""
    config = load_config(git_root)
    if not config.ai_command or not config.ide_command:
        setup_cmd.main([], standalone_mode=False)
        config = load_config(git_root)
    return config
