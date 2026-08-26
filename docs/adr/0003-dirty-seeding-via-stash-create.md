# --dirty seeds sandboxes via git stash create

`--dirty` replicates the host repo's uncommitted state into a new worktree with
`git stash create` (host repo) + `git stash apply --index` (worktree), plus a
copy of untracked files. Unlike `git diff | git apply`, this writes only objects
(the host tree, index, and refs stay byte-for-byte untouched), preserves the
staged/unstaged split exactly, and handles binaries, modes, and deletions as
real git objects; the worktree shares the object database, so the stash commit
applies directly. Conflicts (possible with a diverged `--from` base) land only
in the disposable sandbox, never on the host. On a diverged base the `--index`
apply can fail; the plain-apply fallback then keeps the changes but collapses
the staged/unstaged split.
