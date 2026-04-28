import dataclasses
import fcntl
import functools
import importlib.resources
import logging
import os
import pathlib
import random
import secrets
import shutil
import string
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext

import click
from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from locki.logging import print_log_tail
from locki.paths import HOME, RUNTIME, WORKTREES, WORKTREES_META
from locki.runes import ERROR, FUTHARK, SUCCESS

logger = logging.getLogger(__name__)


class AliasGroup(click.Group):
    """Click group that supports pipe-separated command aliases (e.g. 'shell | sh | bash')."""

    def get_command(self, ctx, cmd_name):
        # Direct match first
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        # Try alias match
        for name in self.list_commands(ctx):
            if cmd_name in name.split(" | "):
                return super().get_command(ctx, name)
        return None

    def format_commands(self, ctx, formatter):
        """Write the commands, showing only the primary name."""
        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            primary = subcommand.split(" | ")[0]
            help_text = cmd.get_short_help_str(limit=formatter.width)
            commands.append((primary, help_text))
        if commands:
            with formatter.section("Commands"):
                formatter.write_dl(commands)


@contextmanager
def spinner(text: str):
    is_tty = sys.stderr.isatty()
    stop = threading.Event()
    start = time.time()

    def _spin():
        while not stop.wait(0.2):
            sys.stderr.write(f"\r{random.choice(FUTHARK)} {text}")
            sys.stderr.flush()

    def _duration() -> str:
        elapsed = int(time.time() - start)
        if elapsed < 5:
            return ""
        s = f" ({elapsed}s)" if elapsed < 60 else f" ({elapsed // 60}m{elapsed % 60}s)"
        return click.style(s, dim=True)

    thread: threading.Thread | None = None
    if is_tty:
        thread = threading.Thread(target=_spin, daemon=True)
        thread.start()
    else:
        sys.stderr.write(f"\n[spinner] {text}")
        sys.stderr.flush()
    try:
        yield
        if thread:
            stop.set()
            thread.join()
        click.echo(
            f"\r{SUCCESS} {text.replace('ing ', 'ed ', count=1)}{_duration()} ",
            err=True,
        )
    except BaseException:
        if thread:
            stop.set()
            thread.join()
        click.echo(f"\r{ERROR} {text} failed{_duration()}", err=True)
        raise
    finally:
        sys.stderr.flush()


def run_command(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    cwd: str = ".",
    check: bool = True,
    input: bytes | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    logger.debug("Command: %s", command)
    with spinner(message) if not quiet else nullcontext():
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                env={**os.environ, **(env or {})},
                cwd=cwd,
                input=input,
            )
            logger.debug("%s", result.stdout.decode(errors="replace").rstrip())
            logger.debug("%s", result.stderr.decode(errors="replace").rstrip())

            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)

            return result
        except FileNotFoundError:
            logger.error("%s is not installed. Please install it first.", command[0])
            sys.exit(1)
        except subprocess.CalledProcessError:
            print_log_tail()
            raise


@functools.cache
def limactl() -> str:
    bundled = importlib.resources.files("locki") / "data" / "bin" / "limactl"
    if bundled.is_file():
        return str(bundled)
    system = shutil.which("limactl")
    if system:
        return system
    logger.error("limactl is not installed. Please install Lima or use a platform-specific locki wheel.")
    sys.exit(1)


def run_in_vm(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    return run_command(
        [limactl(), "shell", "--start", "--preserve-env", "--tty=false", "locki", "--", "sudo", "-E", *command],
        message,
        env=env,
        cwd="/",
        input=input,
        check=check,
        quiet=quiet,
    )


@contextmanager
def file_lock(name: str, wait_message: str):
    """Acquire an exclusive file lock."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock_path = RUNTIME / f"{name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            with spinner(wait_message):
                fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@functools.cache
def git_root() -> pathlib.Path:
    cwd = pathlib.Path.cwd().resolve()
    if cwd.is_relative_to(WORKTREES.resolve()):
        wt_path = WORKTREES / cwd.relative_to(WORKTREES).parts[0]
        meta_git = WORKTREES_META / wt_path.name / ".git"
        if not meta_git.exists():
            logger.error("No worktree metadata found for '%s'.", wt_path.name)
            sys.exit(1)
        (wt_path / ".git").write_text(meta_git.read_text())
        result = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("Could not determine main repo from worktree metadata.")
            sys.exit(1)
        return pathlib.Path(result.stdout.strip()).parent
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Not inside a git repository.")
        sys.exit(1)
    return pathlib.Path(result.stdout.strip())


def current_worktree() -> pathlib.Path | None:
    """If cwd is inside a Locki-managed worktree, return its path."""
    cwd = pathlib.Path.cwd().resolve()
    if not cwd.is_relative_to(WORKTREES.resolve()):
        return None
    return WORKTREES / cwd.relative_to(WORKTREES).parts[0]


def pretty_path(p: pathlib.Path) -> str:
    try:
        return "~/" + str(p.relative_to(HOME))
    except ValueError:
        return str(p)


def format_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers)]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


GIT_HOOKS = [
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-commit",
    "pre-merge-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-push",
    "pre-receive",
    "update",
    "proc-receive",
    "post-receive",
    "post-update",
    "reference-transaction",
    "push-to-checkout",
    "pre-auto-gc",
    "post-rewrite",
    "sendemail-validate",
    "fsmonitor-watchman",
]


def setup_worktree_hooks(repo: pathlib.Path, meta_dir: pathlib.Path, wt_path: pathlib.Path) -> None:
    run_command(
        ["git", "-C", str(repo), "config", "extensions.worktreeConfig", "true"],
        "Enabling per-worktree git config",
    )
    hooks_dir = meta_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_script = (importlib.resources.files("locki") / "data" / "locki-hook.sh").read_bytes()
    for name in GIT_HOOKS:
        hook_path = hooks_dir / name
        hook_path.write_bytes(hook_script)
        hook_path.chmod(0o755)
    run_command(
        ["git", "-C", str(wt_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
        "Configuring per-worktree hooks",
    )
    run_command(
        ["git", "-C", str(wt_path), "config", "--worktree", "push.autoSetupRemote", "true"],
        "Configuring auto push for new branches",
    )


def gen_id() -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))


# ── Sandbox discovery (repo-agnostic) ────────────────────────────────────────


@dataclasses.dataclass
class IncludeInfo:
    name: str  # basename used as directory name in .locki/include/
    repo: pathlib.Path
    branch: str


@dataclasses.dataclass
class SandboxInfo:
    wt_id: str
    branch: str
    repo: pathlib.Path
    include: list[IncludeInfo] = dataclasses.field(default_factory=list)

    @property
    def wt_path(self) -> pathlib.Path:
        return WORKTREES / self.wt_id

    @property
    def meta_path(self) -> pathlib.Path:
        return WORKTREES_META / self.wt_id

    def include_wt_path(self, name: str) -> pathlib.Path:
        return self.wt_path / ".locki" / "include" / name

    def include_meta_path(self, name: str) -> pathlib.Path:
        return self.meta_path / "include" / name


def live_branch(meta_dir: pathlib.Path) -> str:
    """Read the worktree's current branch via its `.git` pointer + `HEAD`.

    Returns `(detached #locki-<wt-id>)` for a detached HEAD, or
    `(broken #locki-<wt-id>)` if the gitdir is gone.  `<wt-id>` is the parent
    sandbox id (the dir directly under `WORKTREES_META`), so include entries
    show the same id as their parent.
    """
    try:
        wt_id = meta_dir.resolve().relative_to(WORKTREES_META.resolve()).parts[0]
    except (ValueError, IndexError):
        wt_id = meta_dir.name
    try:
        gitdir_line = (meta_dir / ".git").read_text().strip()
        if gitdir_line.startswith("gitdir:"):
            gitdir = pathlib.Path(gitdir_line.split(":", 1)[1].strip())
            head = (gitdir / "HEAD").read_text().strip()
            if head.startswith("ref: refs/heads/"):
                return head.removeprefix("ref: refs/heads/")
            return f"(detached #locki-{wt_id})"
    except OSError:
        pass
    return f"(broken #locki-{wt_id})"


def list_sandboxes() -> list[SandboxInfo]:
    """Every Locki sandbox on disk, read from the meta directory."""
    if not WORKTREES_META.exists():
        return []
    sandboxes: list[SandboxInfo] = []
    for meta_dir in sorted(WORKTREES_META.iterdir()):
        if not meta_dir.is_dir() or not (meta_dir / "repo").exists():
            continue
        include: list[IncludeInfo] = []
        include_root = meta_dir / "include"
        if include_root.is_dir():
            for inc_dir in sorted(include_root.iterdir()):
                if inc_dir.is_dir() and (inc_dir / "repo").exists():
                    include.append(
                        IncludeInfo(
                            name=inc_dir.name,
                            repo=pathlib.Path((inc_dir / "repo").read_text().strip()),
                            branch=live_branch(inc_dir),
                        )
                    )
        sandboxes.append(
            SandboxInfo(
                wt_id=meta_dir.name,
                branch=live_branch(meta_dir),
                repo=pathlib.Path((meta_dir / "repo").read_text().strip()),
                include=include,
            )
        )
    return sandboxes


def cwd_git_repo() -> pathlib.Path | None:
    """Return the git repo relevant to cwd, or None if cwd is outside every repo.

    Inside a Locki worktree (or include), returns the sandbox's *primary* repo so
    scoping ("sandboxes of this repo") matches the user's intent.  Otherwise
    returns `git rev-parse --show-toplevel`.
    """
    wt_path = current_worktree()
    if wt_path is not None:
        meta_repo = WORKTREES_META / wt_path.name / "repo"
        if meta_repo.exists():
            return pathlib.Path(meta_repo.read_text().strip()).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return pathlib.Path(result.stdout.strip()).resolve()



def _new_sandbox(repo: pathlib.Path) -> SandboxInfo:
    wt_id = gen_id()
    return SandboxInfo(wt_id=wt_id, branch=f"untitled#locki-{wt_id}", repo=repo)


def resolve_sandbox(
    match: str | None,
    interactive: bool,
    create: str = "allow",
    filter_out_current_repo: bool = False,
) -> SandboxInfo:
    """Pick or create a sandbox.

    *create* controls sandbox creation:
      - ``"force"``: always create a new sandbox (cwd must be in a git repo).
      - ``"allow"``: show "create new" in the interactive picker.
      - ``"deny"``: only existing sandboxes.

    *match* resolution order (first non-empty wins):
      1. wt_id prefix across all sandboxes.
      2. Branch substring on current-repo sandboxes.
      3. Branch substring on all sandboxes.

    Implicit behavior:
      - Inside a Locki-managed worktree (no `match`, no `interactive`, not filtering out this
        repo): return the current sandbox directly.
    """
    cwd_repo = cwd_git_repo()

    if create == "force":
        if cwd_repo is None:
            click.echo(f"{ERROR} Cannot create a sandbox outside a git repo.", err=True)
            sys.exit(1)
        return _new_sandbox(cwd_repo)

    all_sandboxes = list_sandboxes()
    cwd_sandbox = next((s for s in all_sandboxes if s.wt_id == wt_path.name), None) if (wt_path := current_worktree()) else None

    if filter_out_current_repo and cwd_repo is None:
        click.echo(
            f"{ERROR} Not inside a git repo.",
            err=True,
        )
        sys.exit(1)

    if filter_out_current_repo:
        candidate_sandboxes = [s for s in all_sandboxes if s.repo.resolve() != cwd_repo.resolve()]  # type: ignore[union-attr]
    elif cwd_repo is not None:
        candidate_sandboxes = [s for s in all_sandboxes if s.repo.resolve() == cwd_repo.resolve()]
    else:
        candidate_sandboxes = all_sandboxes

    if match is not None:
        matches = [s for s in all_sandboxes if s.wt_id.startswith(match)] or [s for s in candidate_sandboxes if match in s.branch] or [s for s in all_sandboxes if match in s.branch]
        match matches:
            case [single_match]:
                return single_match
            case []:
                click.echo(
                    f"{ERROR} No sandbox matching {click.style(match, fg='yellow')!r}.",
                    err=True,
                )
                sys.exit(1)
            case _:
                click.echo(
                    f"{ERROR} Ambiguous match for {click.style(match, fg='yellow')!r}: {", ".join(s.branch for s in matches)}",
                    err=True,
                )
                sys.exit(1)

    if cwd_sandbox is not None and not interactive and not filter_out_current_repo:
        return cwd_sandbox

    allow_create = create == "allow" and cwd_repo is not None and not filter_out_current_repo
    if not sys.stdin.isatty():
        hint = " or --create" if allow_create else ""
        click.echo(
            f"{ERROR} No sandbox specified. Use -m <query>{hint} in non-interactive mode.",
            err=True,
        )
        sys.exit(1)

    by_id = {s.wt_id: s for s in all_sandboxes}
    scope_all = cwd_repo is None
    while True:
        choices: list = []
        if allow_create:
            choices.append(Choice(value="__create__", name="(create new)"))
        for s in sorted(candidate_sandboxes, key=lambda x: x.branch):
            label = s.branch + (f" ({pretty_path(s.repo)})" if scope_all else "")
            choices.append(Choice(value=s.wt_id, name=label))
        if not scope_all and not filter_out_current_repo:
            choices.append(Choice(value="__all__", name="(show sandboxes from all repos)"))

        if not choices:
            click.echo(f"{ERROR} No matching sandboxes.", err=True)
            sys.exit(1)

        selected = inquirer.fuzzy(message="Select a sandbox:", choices=choices).execute()

        if selected == "__create__":
            assert cwd_repo is not None
            return _new_sandbox(cwd_repo)
        if selected == "__all__":
            candidate_sandboxes = all_sandboxes
            scope_all = True
            continue
        return by_id[selected]
