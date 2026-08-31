"""Sandbox worktrees — the host-side git half of a sandbox.

A sandbox is a worktree plus its Incus container (services.container); a
worktree can exist without a container (e.g. after `locki vm delete`).
"""

import dataclasses
import functools
import pathlib
import secrets
import shutil
import subprocess
import sys

import click

from locki.paths import PACKAGE_DATA, WORKTREES, WORKTREES_META, XDG_CONFIG, guest_worktree_path
from locki.services.home import home
from locki.utils import fail, pretty_path, run_command

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

WT_ID_LEN = 8


# The `<repo>-locki-<id>` / `<stem>#locki-<id>` naming convention, single-sourced:

WT_DIR_TAG = "-locki-"


def wt_dir_name(repo_name: str, wt_id: str) -> str:
    return f"{repo_name}{WT_DIR_TAG}{wt_id}"


def wt_id_from_dir(dir_name: str) -> str:
    return dir_name[-WT_ID_LEN:]


BRANCH_TAG = "#locki-"


def branch_suffix(wt_id: str) -> str:
    return f"{BRANCH_TAG}{wt_id}"


@dataclasses.dataclass
class IncludeInfo:
    name: str  # basename used as directory name in .locki/include/
    repo: pathlib.Path
    branch: str


@dataclasses.dataclass
class WorktreeInfo:
    wt_id: str
    branch: str
    repo: pathlib.Path
    wt_dir: str = ""
    include: list[IncludeInfo] = dataclasses.field(default_factory=list)

    def __post_init__(self):
        if not self.wt_dir:
            self.wt_dir = wt_dir_name(self.repo.name, self.wt_id)

    @property
    def path(self) -> pathlib.Path:
        return WORKTREES / self.wt_dir

    @property
    def sandbox_path(self) -> pathlib.PurePosixPath:
        """The worktree path as seen by the Lima VM and sandbox container."""
        return guest_worktree_path(self.path)

    @property
    def meta_path(self) -> pathlib.Path:
        return WORKTREES_META / self.wt_dir

    def include_path(self, name: str) -> pathlib.Path:
        return self.path / ".locki" / "include" / name

    def include_meta_path(self, name: str) -> pathlib.Path:
        return self.meta_path / "include" / name

    def as_dict(self) -> dict:
        return {
            "id": self.wt_id,
            "branch": self.branch,
            "path": str(self.path),
            "repo": str(self.repo),
            "include": [{"name": i.name, "repo": str(i.repo), "branch": i.branch} for i in self.include],
        }


@functools.cache
def _merged_branches(repo: str, trunk: str) -> frozenset[str]:
    """All branches merged into *trunk* — one git call answers the common case
    for every sandbox of the repo."""
    result = run_command(
        ["git", "-C", repo, "branch", "--merged", trunk, "--format=%(refname:short)"],
        "Listing merged branches",
        check=False,
        quiet=True,
    )
    return frozenset(result.stdout.decode().split())


def _matching_branches(repo: str, wt_id: str) -> list[str]:
    result = run_command(
        ["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/*{branch_suffix(wt_id)}"],
        "Listing sandbox branches",
        check=False,
        quiet=True,
    )
    return result.stdout.decode().split()


class WorktreeService:
    """Git worktrees + metadata: the host-side half of a sandbox."""

    def add(
        self,
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
        branch = branch or f"untitled{branch_suffix(wt_id)}"
        dir_name = wt_dir_name(repo.name, wt_id)
        if parent_name is None:
            wt_path = WORKTREES / dir_name
            meta_path = WORKTREES_META / dir_name
        else:
            parent_dir = wt_dir_name(parent_name, wt_id)
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
            or XDG_CONFIG / "git" / "ignore"
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

    def create(self, worktree: WorktreeInfo, from_ref: str | None = None) -> None:
        run_command(
            ["git", "-C", str(worktree.repo), "worktree", "prune"],
            "Pruning stale git worktrees",
            print_success=False,
        )
        self.add(worktree.repo, worktree.wt_id, branch=worktree.branch, from_ref=from_ref)
        locki_dir = worktree.path / ".locki"
        locki_dir.mkdir(parents=True, exist_ok=True)
        (locki_dir / ".gitignore").write_text("*\n")
        (locki_dir / "tmp").mkdir(exist_ok=True)

    def fix_branches(self, worktree: WorktreeInfo) -> None:
        """Rename manually-switched branches to carry the sandbox's #locki-<id> suffix."""
        suffix = branch_suffix(worktree.wt_id)
        for meta_dir, wt_path in [
            (worktree.meta_path, worktree.path),
            *(
                (worktree.include_meta_path(i.name), worktree.include_path(i.name))
                for i in worktree.include
                if worktree.include_path(i.name).exists() and worktree.include_meta_path(i.name).exists()
            ),
        ]:
            branch = self.live_branch(meta_dir)
            if branch.startswith("(") or branch.endswith(suffix):
                continue
            new_branch = f"{branch.split(BRANCH_TAG)[0]}{suffix}"
            run_command(
                ["git", "-C", str(wt_path), "checkout", "-B", new_branch],
                f"Fixing branch to {click.style(new_branch, fg='green')}",
            )

    def remove(self, worktree: WorktreeInfo, *, branches: bool) -> None:
        """Remove the worktree, its includes, and metadata; optionally its branches.
        The container half is ContainerService.remove's job."""
        for inc in worktree.include:
            inc_wt = worktree.include_path(inc.name)
            run_command(
                ["git", "-C", str(inc.repo), "worktree", "remove", "--force", str(inc_wt)],
                f"Removing include worktree {inc.name}",
                check=False,
            )
            run_command(
                ["git", "-C", str(inc.repo), "worktree", "prune"],
                f"Pruning {inc.repo.name}",
                check=False,
            )

        shutil.rmtree(worktree.path, ignore_errors=True)
        shutil.rmtree(worktree.meta_path, ignore_errors=True)
        run_command(
            ["git", "-C", str(worktree.repo), "worktree", "prune"],
            "Pruning primary worktree",
            check=False,
        )

        if branches:
            for repo in [*(inc.repo for inc in worktree.include), worktree.repo]:
                for b in _matching_branches(str(repo), worktree.wt_id):
                    run_command(
                        ["git", "-C", str(repo), "branch", "-D", b],
                        f"Removing branch {b}",
                        check=False,
                    )

    def trunk(self, repo: pathlib.Path) -> str | None:
        """The repo's trunk branch: origin/HEAD's target, falling back to main/master."""
        ref = run_command(
            ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
            "Reading origin HEAD",
            check=False,
            quiet=True,
        )
        if ref.returncode == 0:
            return ref.stdout.decode().strip().removeprefix("refs/remotes/origin/")
        return next(
            (
                name
                for name in ("main", "master")
                if run_command(
                    ["git", "-C", str(repo), "rev-parse", "--verify", name],
                    "Checking trunk candidate",
                    check=False,
                    quiet=True,
                ).returncode
                == 0
            ),
            None,
        )

    def is_merged(self, repo: pathlib.Path, trunk: str, branch: str) -> bool:
        """Whether *branch* is merged into *trunk*, including squash merges."""
        if branch in _merged_branches(str(repo), trunk):
            return True

        def git(*args: str) -> subprocess.CompletedProcess[bytes]:
            return run_command(["git", "-C", str(repo), *args], "Checking merge status", check=False, quiet=True)

        merge_base = git("merge-base", trunk, branch)
        if merge_base.returncode != 0:
            return False
        tree = git("rev-parse", f"{branch}^{{tree}}")
        if tree.returncode != 0:
            return False
        squash_commit = git(
            "commit-tree", tree.stdout.decode().strip(), "-p", merge_base.stdout.decode().strip(), "-m", "squash check"
        )
        if squash_commit.returncode != 0:
            return False
        cherry = git("cherry", trunk, squash_commit.stdout.decode().strip())
        return cherry.returncode == 0 and cherry.stdout.decode().strip().startswith("-")

    def has_uncommitted_changes(self, worktree: WorktreeInfo, *, quiet: bool = False) -> bool:
        return bool(
            run_command(
                ["git", "-C", str(worktree.path), "status", "--porcelain"],
                "Checking for uncommitted changes",
                check=False,
                quiet=quiet,
            ).stdout.strip()
        )

    def current_worktree(self) -> pathlib.Path | None:
        """If cwd is inside a Locki-managed worktree, return its path."""
        cwd = pathlib.Path.cwd().resolve()
        if not cwd.is_relative_to(WORKTREES.resolve()):
            return None
        return WORKTREES / cwd.relative_to(WORKTREES).parts[0]

    def live_branch(self, meta_dir: pathlib.Path) -> str:
        """Read the worktree's current branch via its `.git` pointer + `HEAD`.

        Returns `(detached #locki-<wt-id>)` for a detached HEAD, or
        `(broken #locki-<wt-id>)` if the gitdir is gone.  `<wt-id>` is the parent
        sandbox id (the dir directly under `WORKTREES_META`), so include entries
        show the same id as their parent.
        """
        try:
            wt_id = wt_id_from_dir(meta_dir.resolve().relative_to(WORKTREES_META.resolve()).parts[0])
        except (ValueError, IndexError):
            wt_id = wt_id_from_dir(meta_dir.name)
        try:
            gitdir_line = (meta_dir / ".git").read_text().strip()
            if gitdir_line.startswith("gitdir:"):
                gitdir = pathlib.Path(gitdir_line.split(":", 1)[1].strip())
                head = (gitdir / "HEAD").read_text().strip()
                if head.startswith("ref: refs/heads/"):
                    return head.removeprefix("ref: refs/heads/")
                return f"(detached {branch_suffix(wt_id)})"
        except OSError:
            pass
        return f"(broken {branch_suffix(wt_id)})"

    def list(self) -> list[WorktreeInfo]:
        """Every Locki sandbox on disk, read from the meta directory.

        Automatically prunes metadata for worktrees that no longer exist on disk
        (e.g. deleted outside Locki).
        """
        if not WORKTREES_META.exists():
            return []
        found: list[WorktreeInfo] = []
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
                                branch=self.live_branch(inc_dir),
                            )
                        )
            found.append(
                WorktreeInfo(
                    wt_id=wt_id_from_dir(meta_dir.name),
                    branch=self.live_branch(meta_dir),
                    repo=pathlib.Path((meta_dir / "repo").read_text().strip()),
                    wt_dir=wt_dir,
                    include=include,
                )
            )
        return found

    @functools.cached_property
    def cwd_repo(self) -> pathlib.Path | None:
        """The git repo relevant to cwd, or None if cwd is outside every repo.

        Inside a Locki worktree (or include), returns the sandbox's *primary* repo so
        scoping ("sandboxes of this repo") matches the user's intent.  Otherwise
        returns `git rev-parse --show-toplevel`.
        """
        wt_path = self.current_worktree()
        if wt_path is not None:
            meta_repo = WORKTREES_META / wt_path.name / "repo"
            if meta_repo.exists():
                return pathlib.Path(meta_repo.read_text().strip()).resolve()
        result = run_command(["git", "rev-parse", "--show-toplevel"], "Resolving repo root", check=False, quiet=True)
        top = result.stdout.decode().strip()
        if result.returncode != 0 or not top:
            return None
        return pathlib.Path(top).resolve()

    def new(self, repo: pathlib.Path, branch_stem: str = "untitled") -> WorktreeInfo:
        wt_id = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(WT_ID_LEN))
        return WorktreeInfo(wt_id=wt_id, branch=f"{branch_stem}{branch_suffix(wt_id)}", repo=repo)

    def resolve(
        self,
        match: str | None,
        interactive: bool,
        create: str = "allow",
        filter_out_current_repo: bool = False,
    ) -> WorktreeInfo:
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
        cwd_repo = self.cwd_repo

        if create == "force":
            if cwd_repo is None:
                fail("Cannot create a sandbox outside a git repo.")
            return self.new(cwd_repo)

        all_sandboxes = self.list()
        cwd_sandbox = (
            next((s for s in all_sandboxes if s.wt_dir == wt_path.name), None)
            if (wt_path := self.current_worktree())
            else None
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
                if title := home.ai_title(s.sandbox_path):
                    label += f" — {title}"
                choices.append(Choice(value=s.wt_id, name=label))
            if not scope_all and not filter_out_current_repo:
                choices.append(Choice(value="__all__", name="(show sandboxes from all repos)"))

            if not choices:
                fail("No matching sandboxes.")

            selected = inquirer.fuzzy(message="Select a sandbox:", choices=choices).execute()

            if selected == "__create__":
                assert cwd_repo is not None
                return self.new(cwd_repo)
            if selected == "__all__":
                candidate_sandboxes = all_sandboxes
                scope_all = True
                continue
            return by_id[selected]


worktrees = WorktreeService()
