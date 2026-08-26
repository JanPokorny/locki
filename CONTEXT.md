# Locki

AI sandboxing on the developer's own machine: each unit of work runs in an isolated
container against an isolated git worktree, with a narrow bridge back to the host.

## Language

**Sandbox**:
A worktree plus its container — the unit users create, enter, stop, and remove.
_Avoid_: environment, workspace, box

**Worktree**:
The host-side git half of a sandbox. Can outlive its container (e.g. after `locki vm delete`).

**Container**:
The Incus half of a sandbox, named by the sandbox id. Disposable — recreated on demand
from the worktree.

**Sandbox id**:
The 8-character slug shared by the worktree directory (`<repo>-locki-<id>`), the branch
suffix (`#locki-<id>`), and the container name.
_Avoid_: treating "worktree id" and "container name" as distinct identifiers — they are
all the same id

**Include**:
A worktree of a second repository grafted into a sandbox under `.locki/include/`.

**Trunk**:
The repo's main branch (origin/HEAD, falling back to main/master); the merge target that
qualifies a sandbox for `rm --merged`.

**Idle**:
A container with no live Incus operation. Idle containers are *stopped*, never deleted.

**Last used**:
When a sandbox was last opened (`ai`/`x`/`cd`/`ide`) or had container activity. Not the
worktree's file mtimes.
_Avoid_: last active (that is the daemon's transient bookkeeping for running containers)

**Orphan**:
A container whose worktree no longer exists on disk; reaped automatically.

**Stop**:
Reversible shutdown of a sandbox's container; worktree, branches, and container survive.
_Avoid_: pause (a different Incus operation — freeze)

**Remove**:
Deletion of a sandbox: container and worktree are gone; branches survive unless asked.
_Avoid_: delete (reserved for `locki vm delete`, which destroys the whole VM)

**Command bridge**:
The host daemon's SSH forced-command proxy that lets sandboxes run an allowlisted set of
host commands (git, gh, port forwarding).
