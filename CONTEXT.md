# Locki

Sandboxed git worktrees for AI agents: each conversation gets a worktree and an
Incus container in a shared Lima VM, isolated from the host repo and home.

## Language

**Sandbox**:
A worktree plus its Incus container; the unit users create, enter, and remove.
_Avoid_: container, VM, environment

**Worktree**:
The host-side git worktree of a sandbox, bind-mounted into its container at the
same absolute path.
_Avoid_: checkout, workspace

**Host repo**:
The user's original clone that worktrees are created from (`WorktreeInfo.repo`).
_Avoid_: source repo, main repo, master folder

**Include**:
A second repo's worktree grafted into a sandbox under `.locki/include/`.

**Trunk**:
The repo's main branch: origin/HEAD's target, falling back to local main/master.

**Bridge**:
The host daemon executing the filtered git/gh/locki command set on behalf of a
sandbox. Anything not in the filter runs host-side only.
_Avoid_: proxy

**Pull / Push**:
File transfer between a worktree and its host repo, 1:1 by relative path --
pull is worktree→host repo, push is host repo→worktree (`locki file`).
Pushed files from outside the repo land in `.locki/tmp/` (no 1:1 path exists).
_Avoid_: sync, copy out/in

**Dirty**:
Carrying uncommitted changes: a dirty worktree makes `locki rm` offer a rescue
(and blocks non-interactive removal); `--dirty` creates a sandbox seeded with
the host repo's uncommitted state. `--raw` is `--dirty` plus gitignored files
(the raw repo folder).

**Rescue**:
The interactive offer in `locki rm` to pull files the removal would destroy.

**Clash**:
A transfer destination whose overwrite would lose content git cannot restore:
untracked with different content, tracked with uncommitted modifications, an
uncommitted deletion, or a directory/symlink standing in the file's place.
A file differing only in the executable bit differs (git tracks that bit).
_Avoid_: conflict (that's git merges), collision
