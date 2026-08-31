"""Host-side file transfer between a sandbox worktree and its host repo.

Paths map 1:1 (worktree-relative == repo-relative). Everything here runs on
the host only and is never reachable through the sandbox command bridge --
a sandbox-initiated pull could write into the host repo
(docs/adr/0001-transfer-commands-are-host-only.md).
"""

import contextlib
import dataclasses
import errno
import filecmp
import os
import pathlib
import shutil
import stat
import typing

import click

from locki.runes import WARNING
from locki.utils import fail, run_command

ClashPolicy = typing.Literal["abort", "overwrite", "skip"]


def _git_lines(root: pathlib.Path, *args: str) -> list[str]:
    result = run_command(["git", "-C", str(root), *args], "Listing files", check=False, quiet=True)
    return [p for p in result.stdout.decode().split("\0") if p]


def untracked_files(root: pathlib.Path, *, include_ignored: bool = False) -> list[str]:
    """All untracked files of *root* (non-ignored ones by default), as relative paths."""
    exclude = [] if include_ignored else ["--exclude-standard"]
    return _git_lines(root, "ls-files", "--others", *exclude, "-z")


def _excluded(rel: pathlib.Path, *, allow_locki_tmp: bool) -> bool:
    """The one exclusion rule: .git never transfers; .locki is sandbox-internal,
    except .locki/tmp where the caller allows it (pulls)."""
    if ".git" in rel.parts:
        return True
    return bool(rel.parts) and rel.parts[0] == ".locki" and not (allow_locki_tmp and rel.parts[:2] == (".locki", "tmp"))


def changed_files(root: pathlib.Path) -> list[str]:
    """Untracked + modified (staged or not) files of *root*, as relative paths.

    Deletions, symlinks (e.g. the sandbox's node_modules/.venv cache links),
    and anything under .locki/ or .git are left out -- only copyable files.
    """
    modified = _git_lines(root, "diff", "--name-only", "-z", "HEAD")
    seen: list[str] = []
    for p in untracked_files(root) + modified:
        if p in seen or _excluded(pathlib.Path(p), allow_locki_tmp=False):
            continue
        full = root / p
        if full.is_symlink() or not full.is_file():
            continue
        seen.append(p)
    return sorted(seen)


def _walk_files(top: pathlib.Path) -> typing.Iterator[pathlib.Path]:
    for dirpath, _dirnames, filenames in os.walk(top, followlinks=False):
        for name in filenames:
            yield pathlib.Path(dirpath) / name


def locki_tmp_files(root: pathlib.Path) -> list[str]:
    """Regular files under the worktree's .locki/tmp/, as worktree-relative paths."""
    tmp = root / ".locki" / "tmp"
    return sorted(str(f.relative_to(root)) for f in _walk_files(tmp) if not f.is_symlink() and f.is_file())


def normalize_paths(paths: tuple[str, ...], src_root: pathlib.Path, *, allow_locki_tmp: bool = False) -> list[str]:
    """Resolve user-given paths to src_root-relative ones, rejecting anything unsafe.

    Paths are interpreted relative to the worktree/repo root; a path that only
    exists relative to cwd (inside *src_root*) is accepted too. Escapes through
    symlinks, .git, and .locki (except .locki/tmp for pulls) are rejected.
    """
    rels: list[str] = []
    for raw in paths:
        p = pathlib.Path(raw)
        candidates = [p] if p.is_absolute() else [src_root / p, pathlib.Path.cwd() / p]
        full = next((c for c in candidates if c.exists() or c.is_symlink()), None)
        if full is None:
            fail(f"Path {click.style(raw, fg='yellow')} does not exist in {src_root}.")
        try:
            # normpath collapses ".." lexically so guards below can't be dodged
            # by spellings like "dir/../.locki/x"
            rel = pathlib.Path(os.path.normpath(full.absolute().relative_to(src_root.absolute())))
        except ValueError:
            rel = None
        if rel is None or ".." in rel.parts or not full.resolve().is_relative_to(src_root.resolve()):
            fail(f"Path {click.style(raw, fg='yellow')} is outside {src_root} (or reaches out through a symlink).")
        if _excluded(rel, allow_locki_tmp=allow_locki_tmp):
            fail(
                f"Refusing to transfer {click.style(raw, fg='yellow')}:"
                f" .git and sandbox-internal .locki paths are never transferred."
            )
        if full.is_symlink():
            fail(f"Refusing to transfer symlink {click.style(raw, fg='yellow')}.")
        rels.append(str(rel))
    return rels


def expand_dirs(src_root: pathlib.Path, rel_paths: list[str]) -> list[str]:
    """Expand directories to their contained regular files; symlinks are skipped."""
    out: list[str] = []
    for rel in rel_paths:
        full = src_root / rel
        if not full.is_dir():
            out.append(rel)
            continue
        for f in _walk_files(full):
            frel = f.relative_to(src_root)
            if f.is_symlink() or _excluded(frel, allow_locki_tmp=rel.startswith(".locki/tmp")):
                click.echo(f"{WARNING} Skipping {frel}", err=True)
                continue
            out.append(str(frel))
    return out


@dataclasses.dataclass
class CopyResult:
    copied: list[str] = dataclasses.field(default_factory=list)
    identical: list[str] = dataclasses.field(default_factory=list)
    clashes: list[str] = dataclasses.field(default_factory=list)


def _is_recoverable(dst_root: pathlib.Path, rel: str) -> bool:
    """Whether overwriting dst_root/rel loses nothing: tracked and clean vs HEAD."""
    tracked = run_command(
        ["git", "-C", str(dst_root), "ls-files", "--error-unmatch", "--", rel],
        "Checking destination tracking",
        check=False,
        quiet=True,
    )
    if tracked.returncode != 0:
        return False
    return _clean_vs_head(dst_root, rel)


def _clean_vs_head(dst_root: pathlib.Path, rel: str) -> bool:
    cmd = ["git", "-C", str(dst_root), "diff", "--quiet", "HEAD", "--", rel]
    return run_command(cmd, "Checking destination cleanliness", check=False, quiet=True).returncode == 0


def _same_mode(a: pathlib.Path, b: pathlib.Path) -> bool:
    """Git tracks only the executable bit -- that is the mode identity."""
    return (a.stat().st_mode & 0o100) == (b.stat().st_mode & 0o100)


def classify(src_root: pathlib.Path, dst_root: pathlib.Path, rel_paths: list[str]) -> CopyResult:
    """Sort *rel_paths* into copyable/identical/clashing without copying anything.

    A clash is a destination whose overwrite would lose content git cannot
    restore: untracked with different content, tracked with uncommitted
    modifications, an uncommitted deletion, a symlink, or a directory.
    The result's `copied` field holds the WOULD-copy set.
    """
    result = CopyResult()
    for rel in rel_paths:
        dst = dst_root / rel
        if dst.is_symlink() or dst.is_dir():
            result.clashes.append(rel)
        elif not dst.exists():
            # recreating a file whose deletion is staged/unstaged would undo it
            (result.copied if _clean_vs_head(dst_root, rel) else result.clashes).append(rel)
        elif filecmp.cmp(src_root / rel, dst, shallow=False) and _same_mode(src_root / rel, dst):
            result.identical.append(rel)
        elif _is_recoverable(dst_root, rel):
            result.copied.append(rel)
        else:
            result.clashes.append(rel)
    return result


def copy_files(
    src_root: pathlib.Path,
    dst_root: pathlib.Path,
    rel_paths: list[str],
    *,
    clash_policy: ClashPolicy = "abort",
    outside: typing.Sequence[pathlib.Path] = (),
) -> CopyResult:
    """Copy *rel_paths* from src_root to dst_root, 1:1.

    Clash handling (see classify): "abort" copies NOTHING when any clash
    exists, "skip" copies only the rest, "overwrite" copies everything except
    directory destinations, which are refused before anything is copied.
    Identical files are skipped.

    *outside* files (host paths with no 1:1 mapping, push only) land in the
    destination's .locki/tmp exchange area under their basename; an existing
    differing file there is a clash -- .locki/tmp is gitignored, so git can
    never restore it.
    """
    plan = classify(src_root, dst_root, rel_paths)
    out_map = {f".locki/tmp/{f.name}": f for f in outside}
    for rel, f in out_map.items():
        dst = dst_root / rel
        if dst.is_symlink() or dst.is_dir():
            plan.clashes.append(rel)
        elif dst.exists() and filecmp.cmp(f, dst, shallow=False) and _same_mode(f, dst):
            plan.identical.append(rel)
        elif dst.exists():
            plan.clashes.append(rel)
        else:
            plan.copied.append(rel)
    result = CopyResult(identical=plan.identical, clashes=plan.clashes)
    to_copy = plan.copied
    if result.clashes:
        if clash_policy == "abort":
            return result
        if clash_policy == "overwrite":
            to_copy += result.clashes
            result.clashes = []

    # refuse type mismatches and escapes upfront so a failure never leaves a partial copy
    if dirs := [rel for rel in to_copy if (dst_root / rel).is_dir() and not (dst_root / rel).is_symlink()]:
        fail(f"Cannot copy over a directory (nothing was copied): {', '.join(dirs)}. Remove it first.")
    for rel in to_copy:
        for p in (dst_root / rel).parents:
            if p == dst_root or not p.is_relative_to(dst_root):
                break
            if p.is_symlink() or (p.exists() and not p.is_dir()):
                fail(
                    f"Cannot copy {rel} (nothing was copied):"
                    f" {p.relative_to(dst_root)} is not a real directory. Remove it first."
                )

    for rel in to_copy:
        try:
            _copy_nofollow(src_root, dst_root, rel, src_file=out_map.get(rel))
        except OSError as e:
            fail(f"Copying {rel} failed ({e.strerror or e}); the tree changed underneath -- rerun.")
        result.copied.append(rel)
    return result


def _open_dir(name: str, dir_fd: int, *, create: bool = False) -> int:
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, dir_fd=dir_fd)
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _copy_nofollow(
    src_root: pathlib.Path, dst_root: pathlib.Path, rel: str, *, src_file: pathlib.Path | None = None
) -> None:
    """Copy one file traversing component-by-component with O_NOFOLLOW, so a live
    sandbox cannot race the validated plan by swapping a path segment for a symlink.

    With *src_file* the source is that (user-given, trusted) host path instead of
    src_root/rel, opened plainly -- only the destination side is sandbox-controlled."""
    parts = pathlib.PurePath(rel).parts
    dfd = os.open(dst_root, os.O_RDONLY | os.O_DIRECTORY)
    opened = [dfd]
    try:
        if src_file is None:
            opened.append(sfd := os.open(src_root, os.O_RDONLY | os.O_DIRECTORY))
            for name in parts[:-1]:
                opened.append(sfd := _open_dir(name, sfd))
                opened.append(dfd := _open_dir(name, dfd, create=True))
            opened.append(src_fd := os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=sfd))
        else:
            for name in parts[:-1]:
                opened.append(dfd := _open_dir(name, dfd, create=True))
            opened.append(src_fd := os.open(src_file, os.O_RDONLY))
        if not stat.S_ISREG(os.fstat(src_fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        wflags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        try:
            dst_fd = os.open(parts[-1], wflags, dir_fd=dfd)
        except OSError as e:
            if e.errno != errno.ELOOP:
                raise
            os.unlink(parts[-1], dir_fd=dfd)  # overwriting a symlink clash drops the link, not its target
            dst_fd = os.open(parts[-1], wflags, dir_fd=dfd)
        opened.append(dst_fd)
        with os.fdopen(os.dup(src_fd), "rb") as s, os.fdopen(os.dup(dst_fd), "wb") as d:
            shutil.copyfileobj(s, d)
        os.fchmod(dst_fd, os.fstat(src_fd).st_mode & 0o777)  # never propagate setuid/setgid to the host
    finally:
        for fd in opened:
            os.close(fd)
