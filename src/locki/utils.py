import dataclasses
import fcntl
import functools
import json
import logging
import os
import pathlib
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext, suppress

import click

from locki.logging import print_log_tail
from locki.paths import HOME, LIMA, PACKAGE_DATA, RUNTIME, SANDBOX_HOME, WORKTREES, WORKTREES_META
from locki.runes import ERROR, FUTHARK, SUCCESS

logger = logging.getLogger(__name__)


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        elif k in result and isinstance(result[k], list) and isinstance(v, list):
            result[k] = result[k] + [x for x in v if x not in result[k]]
        else:
            result[k] = v
    return result


def fail(msg: str):
    click.echo(f"{ERROR} {msg}", err=True)
    sys.exit(1)


class AliasGroup(click.Group):
    """Click group that supports pipe-separated command aliases (e.g. 'shell | sh | bash')."""

    def get_command(self, ctx, cmd_name):
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        for name in self.list_commands(ctx):
            if cmd_name in name.split(" | "):
                return super().get_command(ctx, name)
        return None

    def format_commands(self, ctx, formatter):
        """Write the commands, showing only the primary name."""
        commands = [
            (name.split(" | ")[0], cmd.get_short_help_str(limit=formatter.width))
            for name in self.list_commands(ctx)
            if (cmd := self.get_command(ctx, name)) and not cmd.hidden
        ]
        if commands:
            with formatter.section("Commands"):
                formatter.write_dl(commands)


def sandbox_options(create: bool = False):
    """Shared `-m/-i[/-n]` sandbox-selection options."""

    def deco(f):
        if create:
            f = click.option("-n", "--new", "create", is_flag=True, default=False, help="Create a new sandbox.")(f)
        f = click.option("-i", "--interactive", is_flag=True, default=False, help="Force interactive picker.")(f)
        return click.option("-m", "--match", default=None, help="Match a sandbox by id prefix or branch substring.")(f)

    return deco


json_option = click.option("--json", "as_json", is_flag=True, default=False, help="Print the result as JSON to stdout.")


@contextmanager
def spinner(text: str, print_success: bool = True):
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
    if sys.stderr.isatty():
        thread = threading.Thread(target=_spin, daemon=True)
        thread.start()
    elif print_success:
        sys.stderr.write(f"\n[spinner] {text}")
        sys.stderr.flush()

    def _stop_spinner():
        if thread:
            stop.set()
            thread.join()

    try:
        yield
    except BaseException:
        _stop_spinner()
        click.echo(f"\r{ERROR} {text} failed{_duration()}", err=True)
        raise
    else:
        _stop_spinner()
        if print_success:
            click.echo(f"\r{SUCCESS} {text.replace('ing ', 'ed ', 1)}{_duration()} ", err=True)
        elif thread:
            sys.stderr.write("\r\033[2K")
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
    print_success: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    logger.debug("Command: %s", command)
    with spinner(message, print_success=print_success) if not quiet else nullcontext():
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
            fail(f"{command[0]} is not installed. Please install it first.")
        except subprocess.CalledProcessError:
            print_log_tail()
            raise


LIMA_ENV = {"LIMA_HOME": str(LIMA)}


@functools.cache
def limactl() -> str:
    bundled = PACKAGE_DATA / "bin" / "limactl"
    if bundled.is_file():
        return str(bundled)
    system = shutil.which("limactl")
    if system:
        return system
    fail("limactl is not installed. Please install Lima or use a platform-specific locki wheel.")


def vm_status() -> str | None:
    """Return the Locki VM status ('Running', 'Stopped', etc.), or None."""
    result = subprocess.run(
        [limactl(), "list", "locki", "--format", "{{.Status}}"],
        capture_output=True,
        text=True,
        env={**os.environ, **LIMA_ENV},
    )
    return result.stdout.strip() or None


def run_in_vm(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
    check: bool = True,
    quiet: bool = False,
    print_success: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return run_command(
        [limactl(), "shell", "--start", "--preserve-env", "--tty=false", "locki", "--", "sudo", "-E", *command],
        message,
        env={**LIMA_ENV, **(env or {})},
        cwd="/",
        input=input,
        check=check,
        quiet=quiet,
        print_success=print_success,
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
    "pre-auto-gc",
    "post-rewrite",
    "sendemail-validate",
    "fsmonitor-watchman",
]


def add_worktree(
    repo: pathlib.Path,
    wt_id: str,
    parent_name: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
) -> pathlib.Path:
    """Create the sandbox worktree of *repo* for *wt_id*: the *branch* (default
    `untitled#locki-<wt-id>`, reused if it already exists), the worktree itself,
    trusted metadata, and per-worktree hooks.  With *parent_name* (the parent
    sandbox repo's name) the worktree becomes an include inside that sandbox;
    without it, the primary worktree.  *from_ref* bases a newly created branch
    on that ref instead of HEAD."""
    branch = branch or f"untitled#locki-{wt_id}"
    dir_name = f"{repo.name}-locki-{wt_id}"
    if parent_name is None:
        wt_path = WORKTREES / dir_name
        meta_path = WORKTREES_META / dir_name
    else:
        parent_dir = f"{parent_name}-locki-{wt_id}"
        wt_path = WORKTREES / parent_dir / ".locki" / "include" / dir_name
        meta_path = WORKTREES_META / parent_dir / "include" / dir_name

    exists = run_command(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        "Checking for existing branch",
        check=False,
        quiet=True,
    )
    if exists.returncode != 0:
        run_command(
            ["git", "-C", str(repo), "branch", branch] + ([from_ref] if from_ref else []),
            f"Creating branch {click.style(branch, fg='green')}",
            print_success=False,
        )
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        ["git", "-C", str(repo), "worktree", "add", str(wt_path), branch],
        f"Creating worktree for {click.style(dir_name, fg='green')}",
    )
    meta_path.mkdir(parents=True, exist_ok=True)
    (meta_path / ".git").write_text((wt_path / ".git").read_text())
    (meta_path / "repo").write_text(str(repo))

    run_command(
        ["git", "-C", str(repo), "config", "extensions.worktreeConfig", "true"],
        "Enabling per-worktree git config",
        print_success=False,
    )
    hooks_dir = meta_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_script = (PACKAGE_DATA / "locki-hook.sh").read_bytes()
    for name in GIT_HOOKS:
        (hooks_dir / name).write_bytes(hook_script)
        (hooks_dir / name).chmod(0o755)
    run_command(
        ["git", "-C", str(wt_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
        "Configuring per-worktree hooks",
        print_success=False,
    )
    run_command(
        ["git", "-C", str(wt_path), "config", "--worktree", "push.autoSetupRemote", "true"],
        "Configuring auto push for new branches",
        print_success=False,
    )

    # Repos often ignore only "node_modules/", which doesn't match the cache
    # symlinks the sandbox creates. info/exclude is shared across worktrees, so
    # use per-worktree core.excludesFile — it overrides the user's global ignore
    # file, hence its content is carried over (snapshot; fine for throwaway worktrees).
    global_ignore = pathlib.Path(
        run_command(
            ["git", "config", "--path", "core.excludesFile"], "Reading global git excludes", check=False, quiet=True
        )
        .stdout.decode()
        .strip()
        or pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config")) / "git" / "ignore"
    )
    exclude = meta_path / "exclude"
    exclude.write_text((global_ignore.read_text() if global_ignore.is_file() else "") + "\nnode_modules\n.venv\n")
    run_command(
        ["git", "-C", str(wt_path), "config", "--worktree", "core.excludesFile", str(exclude)],
        "Excluding sandbox cache symlinks from git",
        print_success=False,
    )

    # mise trust is per-path, so a trusted root checkout doesn't cover its worktrees
    if mise := shutil.which("mise"):
        show = run_command([mise, "trust", "--show"], "Checking mise trust", cwd=str(repo), check=False, quiet=True)
        if any(line.endswith(": trusted") for line in show.stdout.decode(errors="replace").splitlines()):
            run_command([mise, "trust"], "Trusting mise config", cwd=str(wt_path), check=False, print_success=False)
    return wt_path


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
    wt_dir: str = ""
    include: list[IncludeInfo] = dataclasses.field(default_factory=list)

    def __post_init__(self):
        if not self.wt_dir:
            self.wt_dir = f"{self.repo.name}-locki-{self.wt_id}"

    @property
    def wt_path(self) -> pathlib.Path:
        return WORKTREES / self.wt_dir

    @property
    def meta_path(self) -> pathlib.Path:
        return WORKTREES_META / self.wt_dir

    def include_wt_path(self, name: str) -> pathlib.Path:
        return self.wt_path / ".locki" / "include" / name

    def include_meta_path(self, name: str) -> pathlib.Path:
        return self.meta_path / "include" / name

    def __iter__(self):
        return iter(
            {
                "id": self.wt_id,
                "branch": self.branch,
                "path": str(self.wt_path),
                "repo": str(self.repo),
                "include": [{"name": i.name, "repo": str(i.repo), "branch": i.branch} for i in self.include],
            }.items()
        )


def live_branch(meta_dir: pathlib.Path) -> str:
    """Read the worktree's current branch via its `.git` pointer + `HEAD`.

    Returns `(detached #locki-<wt-id>)` for a detached HEAD, or
    `(broken #locki-<wt-id>)` if the gitdir is gone.  `<wt-id>` is the parent
    sandbox id (the dir directly under `WORKTREES_META`), so include entries
    show the same id as their parent.
    """
    try:
        wt_id = meta_dir.resolve().relative_to(WORKTREES_META.resolve()).parts[0][-8:]
    except (ValueError, IndexError):
        wt_id = meta_dir.name[-8:]
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


def ai_title(sandbox: SandboxInfo) -> str:
    """Last AI-generated session title from the sandbox's Claude Code transcripts, or "".

    Claude Code appends `{"type":"ai-title","aiTitle":...}` lines to
    `~/.claude/projects/<munged-cwd>/<session>.jsonl`; the sandbox's `/root` is
    SANDBOX_HOME, so those are directly readable here. Internal format
    (observed on 2.1.212) -- fail soft on any surprise.
    """
    project = SANDBOX_HOME / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(sandbox.wt_path))
    for jsonl in sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        title = ""
        try:
            # ceiling: full scan, transcripts reach MBs; upgrade: tail-read (title recurs every ~25 lines)
            lines = jsonl.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if '"type":"ai-title"' in line:
                with suppress(json.JSONDecodeError):  # torn line from a live append
                    title = json.loads(line).get("aiTitle") or title
        if title:
            return title
    return ""


def list_sandboxes() -> list[SandboxInfo]:
    """Every Locki sandbox on disk, read from the meta directory.

    Automatically prunes metadata for worktrees that no longer exist on disk
    (e.g. deleted outside Locki).
    """
    if not WORKTREES_META.exists():
        return []
    sandboxes: list[SandboxInfo] = []
    for meta_dir in sorted(WORKTREES_META.iterdir()):
        if not meta_dir.is_dir() or not (meta_dir / "repo").exists():
            continue
        wt_dir = meta_dir.name
        if not (WORKTREES / wt_dir).exists():
            shutil.rmtree(meta_dir, ignore_errors=True)
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
                wt_id=meta_dir.name[-8:],
                branch=live_branch(meta_dir),
                repo=pathlib.Path((meta_dir / "repo").read_text().strip()),
                wt_dir=wt_dir,
                include=include,
            )
        )
    return sandboxes


@functools.cache
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


def new_sandbox(repo: pathlib.Path, branch_stem: str = "untitled") -> SandboxInfo:
    wt_id = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))
    return SandboxInfo(wt_id=wt_id, branch=f"{branch_stem}#locki-{wt_id}", repo=repo)


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

    if sum([create == "force", match is not None, interactive]) > 1:
        fail("--new, --match, and --interactive are mutually exclusive.")

    if create == "force":
        if cwd_repo is None:
            fail("Cannot create a sandbox outside a git repo.")
        return new_sandbox(cwd_repo)

    all_sandboxes = list_sandboxes()
    cwd_sandbox = (
        next((s for s in all_sandboxes if s.wt_dir == wt_path.name), None) if (wt_path := current_worktree()) else None
    )

    if filter_out_current_repo and cwd_repo is None:
        fail("Not inside a git repo.")

    if filter_out_current_repo:
        candidate_sandboxes = [s for s in all_sandboxes if s.repo.resolve() != cwd_repo.resolve()]  # type: ignore[union-attr]
    elif cwd_repo is not None:
        candidate_sandboxes = [s for s in all_sandboxes if s.repo.resolve() == cwd_repo.resolve()]
    else:
        candidate_sandboxes = all_sandboxes

    if match is not None:
        matches = (
            [s for s in all_sandboxes if s.wt_id.startswith(match)]
            or [s for s in candidate_sandboxes if match in s.branch]
            or [s for s in all_sandboxes if match in s.branch]
        )
        match matches:
            case [single_match]:
                return single_match
            case []:
                fail(f"No sandbox matching {click.style(repr(match), fg='yellow')}.")
            case _:
                fail(
                    f"Ambiguous match for {click.style(repr(match), fg='yellow')}: {', '.join(s.branch for s in matches)}"
                )

    if cwd_sandbox is not None and not interactive and not filter_out_current_repo:
        return cwd_sandbox

    allow_create = create == "allow" and cwd_repo is not None and not filter_out_current_repo
    if not sys.stdin.isatty():
        hint = " or --new" if allow_create else ""
        fail(f"No sandbox specified. Use -m <query>{hint} in non-interactive mode.")

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    by_id = {s.wt_id: s for s in all_sandboxes}
    scope_all = cwd_repo is None
    while True:
        choices: list = []
        if allow_create:
            choices.append(Choice(value="__create__", name="(create new)"))
        for s in sorted(candidate_sandboxes, key=lambda x: x.branch):
            label = s.branch + (f" ({pretty_path(s.repo)})" if scope_all else "")
            if title := ai_title(s):
                label += f" — {title}"
            choices.append(Choice(value=s.wt_id, name=label))
        if not scope_all and not filter_out_current_repo:
            choices.append(Choice(value="__all__", name="(show sandboxes from all repos)"))

        if not choices:
            fail("No matching sandboxes.")

        selected = inquirer.fuzzy(message="Select a sandbox:", choices=choices).execute()

        if selected == "__create__":
            assert cwd_repo is not None
            return new_sandbox(cwd_repo)
        if selected == "__all__":
            candidate_sandboxes = all_sandboxes
            scope_all = True
            continue
        return by_id[selected]
