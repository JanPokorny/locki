<div align="center">
    <h1>
        <table>
        <tr>
            <th align="center" width="50">L</th>
            <th align="center" width="50">O</th>
            <th align="center" width="50">C</th>
            <th align="center" width="50">K</th>
            <th align="center" width="50">I</th>
        </tr>
        <tr>
            <td align="center">ᛚ</td>
            <td align="center">ᛟ</td>
            <td align="center">ᚲ</td>
            <td align="center">ᚴ</td>
            <td align="center">ᛁ</td>
        </tr>
        </table>
    </h1>
</div>

<p align="center">All-in-one sandboxed worktrees for you and your AIs</p>

<div align=center>
  
![](https://badgen.net/badge/_/macOS%20%E2%9C%94/green?icon=apple&label)
![](https://badgen.net/badge/_/Linux%20%E2%9C%94/green?icon=linux&label)
![](https://badgen.net/badge/_/uv%20tool%20install%20locki/DE5FE9?icon=uv&label)
[![](https://img.shields.io/pypi/v/locki?color=DE5FE9&label=PyPI)](https://pypi.org/project/locki/)
![](https://img.shields.io/pypi/l/locki?color=green&label=License)
  
</div>

&nbsp;

With **Locki**, every AI conversation (Claude Code, Codex, etc.) receives its own *Git worktree* and *VM sandbox* -- zero interference between agents, zero interference with the host machine. Locki uses a unique sandboxing solution based on [Lima](https://lima-vm.io/) and [Incus](https://linuxcontainers.org/), combining the **power of real VMs** with the **speed of containers**. Unlike most sandboxing solutions, Locki can run mostly anything -- from simple Python and Node.js apps to **systemd services**, **containerized applications** and full **Kuberentes clusters**!

&nbsp;

https://github.com/user-attachments/assets/27c0aeb2-c5ef-4e7f-a293-519499215cc8

&nbsp;

- **AI-agnostic**: Supports Claude Code, Codex, Antigravity, Pi, Copilot, OpenCode and more.
- **Fast**: After the initial VM setup, spawning a new sandbox takes seconds.
- **Worktree-backed**: Code lives in a git worktree on disk, fully under your control.
- **No compromises**: Each agent gets a brand new, full-featured machine to develop in.
- **Aggresively cached**: PyPI, NPM, Docker Hub and more are cached to ensure fast builds in a fresh sandbox.
- **Safe Git**: Agents are only able to modify namespaced branches. Stash is scoped. Hooks are redirected.
- **Agent-friendly**: Bundled hand-picked tools and sandbox-specific instructions for best behavior.

**Real-world projects** are using Locki, including [Agent Stack](https://github.com/i-am-bee/agentstack), [Kagenti ADK](https://github.com/kagenti/adk), and [DAM](https://github.com/dam-agents/dam).

&nbsp;

## Get Started

1. Install: `uv tool install locki`. ([Install uv](https://docs.astral.sh/uv/getting-started/installation/) first if you don't have it.)
1. If on Linux, install [QEMU](https://www.qemu.org/download/#linux). (Not needed on macOS.)
1. `cd` to your Git repository, run: `locki ai`, follow interactive setup and choose to create a new sandbox. Wait a few minutes for the initial start.
1. Follow prompts to log in to the AI CLI. Login will be persisted across sandboxes.
1. Build! Your agent is already instructed on how to behave in the sandbox. Agent can create any branch with `#locki-<worktree-id>` suffix, and if `gh` is available on host, even create a pull request.
1. After merging the branch, run `locki rm` to delete the worktree.

&nbsp;

## Quick Reference

Commands act on the current worktree if inside one, letting you select interactively otherwise.

Most important commands are:

```bash
locki ai          # open AI agent in sandbox (pick existing or new)
locki x           # open Bash in sandbox (pick existing or new)
locki file pull   # copy files from a sandbox into your repo
locki rm          # remove sandbox
```

See the "pro-tips" section below for more advanced usage like IDE integration, port forwarding, working on multiple repos at once and more! You can also use `locki --help` anytime for a refresher.

&nbsp;

## Path mapping from host to sandbox

Each sandbox gets its own [worktree](https://git-scm.com/docs/git-worktree) (a full copy of your repo) and shares a common home folder with other sandboxes. The original repo and your actual home stay safely out of reach:

- **Your Git repo** (`~/myproject/`) — ❌ Not visible from any sandbox. That means sandboxes can't reach the `.git` folder and mess it up -- all `git` calls go through a command bridge and get reviewed and filtered.
- **Each sandbox's worktree** (`~/.local/share/locki/worktrees/myproject-locki-.../`) — Visible in corresponding sandbox, at the same path. All edits in the sandbox are instantly visible on host.
- **Shared sandbox home** (`~/.local/share/locki/home/`) — Visible from every sandbox as `~`. Save your agent configuration here to use it in sandboxes.
- **Your actual home** (`~`) — ❌ Not visible from any sandbox. Sandboxes can't mess up your global config.

&nbsp;

## Pro-tips for power users

- Launch an IDE in the worktree folder using `locki ide`. 30+ editors are recognized out of the box (VSCode, Cursor, Zed, the JetBrains suite, Neovim, Sublime Text, ...), and you can set any custom command via `locki setup` or `ide_command`.\
  *(The IDE runs on host: you still need to run `locki ai` / `locki x -- <cmd>` in the built-in terminal to run commands in the sandbox. This is intentional: running your IDE inside the sandbox (using "remote SSH" or similar features) is unsafe, since the agent could potentially access authentication tokens stored in the IDE's memory.)*

- When `cd`'d into a worktree folder (`~/.local/share/locki/worktrees/.../`), `locki` commands use it by default -- otherwise they show an interactive picker. Use `--match`/`-m` to select by sandbox id or branch substring. `locki list` (alias `ls`) shows every sandbox and its worktree path.

- Copy files between a sandbox and your repo (the host repo) with `locki file pull` / `locki file push` (modeled on `incus file`) -- paths map 1:1, so `locki file pull tools/gen.py` lands at `tools/gen.py` in your repo. Run bare to pick files from a checklist, including the sandbox's `.locki/tmp/` artifacts (screenshots, debug dumps). A source outside your repo -- `locki file push /tmp/dump.bin` -- has no 1:1 path and lands in the sandbox's `.locki/tmp/` exchange area instead. A destination holding uncommitted content is never overwritten without `--force`.

- Start a sandbox from your uncommitted state with `--dirty` (`locki new --dirty`, `locki ai --dirty`, ...): staged, unstaged, and untracked files are replicated into the fresh worktree; your repo is left untouched. Without `-m`/`-i`, `--dirty`/`--raw` implies `-n` -- they can only seed a new sandbox. `--raw` goes one further and also copies gitignored files (`.env`, caches, ...) -- the raw repo folder, which can be big. Careful: gitignored files often hold credentials, and anything copied in is usable by the sandboxed agent -- the command bridge scopes what `git push` can touch, but it can't police what a leaked token allows.

- `locki rm` on a sandbox with unsaved work (uncommitted changes, `.locki/tmp/` artifacts, dirty includes) lists what would be lost and offers to pull it into your repo first -- a pre-checked file picker, so unchecking a file accepts its loss. Files whose identical copy already sits at the host path (e.g. after a `locki file pull`) don't count as unsaved. Non-interactive runs (scripts, `--json`) fail instead of prompting when uncommitted changes would be lost; leftover `.locki/tmp/` artifacts alone never block them. `locki rm --merged` is batch cleanup and never prompts: it skips sandboxes with uncommitted changes and only warns about `.locki/tmp/` artifacts it destroys.

- Editors like VSCode show worktrees in the sidebar, useful as a quick UI for reviewing and modifying changes.\
  *(⚠️ VSCode 1.115.0+ requires setting `"git.detectWorktrees": true` for this to work.)*

- Working on two repos at once? `cd` into your sandbox's primary repo and run `locki include --repo ../other-repo` to graft the other repo into the current sandbox at `.locki/include/<repo-name>-locki-<sandbox-id>/`. Or from the other repo: `locki include --this -m <sandbox-id>`.

- While `locki ai` opens a coding agent, `locki exec` (or short `locki x`) is the low-level version which can run any command. Pass a command to run in a sandbox, use `--match`/`-m` to select by branch substring or sandbox id: `locki exec -m big-refactor -- pytest`.

- The first `locki ai` run prompts you to pick a default harness and editor. Re-run `locki setup` to change them, or edit `~/.config/locki/config.toml` directly — the keys are full command lines, e.g. `ai_command = "agy --dangerously-skip-permissions -c"` and `ide_command = "code ."`. A repo can override `ai_command` via a `locki.toml` in its root; `ide_command` is user-only (it launches on your host).

- Ask your agent to forward ports, or use `locki port-forward` for more control.

- Locki sandboxes provide [Mise](https://mise.jdx.dev) for tool version management -- replacing `nvm`, `rbenv`, `brew` etc. with a single tool. Adding `mise.toml` to your repo with tool versions and task definitions will help agents and humans alike: ask your agent to do it!

- Want to use custom AI configuration in the VM -- instructions, skills, MCP servers, ...? Sandboxes share a home folder accessible at `~/.local/share/locki/home` on host (or `$XDG_DATA_HOME/locki/home`). For example, you can edit `~/.local/share/locki/home/.claude/CLAUDE.md` for sandbox-specific instructions.

- Something is broken? Try `locki vm delete` -- it will preserve your worktrees and settings, but the VM and sandboxes will be recreated from scratch on next run.

- Sandboxes run on Fedora 44. Want a different OS? Create a `locki.toml` file in repo root referencing either [an available OS image](https://images.linuxcontainers.org/), or a local Incus image archive by path. For the local archive format, see the [Incus image format documentation](https://linuxcontainers.org/incus/docs/main/reference/image_format/). Example:

  ```toml
  # locki.toml
  incus_image = "images:ubuntu/questing"
  ```

  For local image archives, use a path (relative to repo root) or a glob pattern. When a glob matches multiple files, the right one is picked by architecture substring (e.g. `arm64`, `x86_64`):

  ```toml
  incus_image = "images/locki-*.tar.xz"
  ```
  <small>(Since containers share a binary cache, it is not recommended to mix `musl` distros (like Alpine) with regular ones.)

&nbsp;

## Comparison

Most sandboxing solutions use one of these techniques:

- **Full VM per sandbox** (Vagrant, Multipass): resource-heavy, slow to start
- **MicroVM per sandbox** (Firecracker, Apple `container`): none or limited support for building, running and orchestrating containers
- **OCI container per sandbox** (Devcontainers, Distrobox, `container-use`): none or limited support for building, running and orchestrating containers; potentially unsafe if running VM-less on Linux
- **OS-level jail** (Landlock, Bubblewrap, `sandbox-exec`): just restriction, not isolation (ports collide, image tags get overwritten, etc.)

Locki instead runs **one Lima VM hosting many lightweight Incus containers** — one shared kernel boundary you can trust, cheap per-sandbox containers, and full support for building and orchestrating containers (even Kubernetes) inside.

&nbsp;

## Security

Locki uses a single Lima VM which can only access the `~/.local/share/locki/worktrees` and `~/.local/share/locki/home` folders (honoring `$XDG_DATA_HOME`), which forms the security boundary. The sandboxed programs can read and write to these folders, and also access anything on the internet and local network. Furthermore, a guest-to-host SSH server exposes a limited set of `git` and `gh` subcommands, with write access restricted to the sandbox's own namespaced branches and stashes (so an agent in one sandbox cannot alter another sandbox's branch, the main branch, or unrelated stashes). `.git` files are checked for tampering when hooks are executed against them.

Locki is designed to provide protection for the host operating system and files from being messed up by a malfunctioning AI agent. There is no exfiltration protection, so be aware that API keys exposed to the agents need to be treated as potentially exposed and disposable, with limited scope. (This is no different from running the agent locally, just specifying that Locki does not help here.)

Locki may not provide perfect security, however it is certainly much better than going full `--yolo` on your bare machine and hoping for the best.

&nbsp;

## How it works

- **Python CLI** driving a single [Lima](https://lima-vm.io/) VM (both `limactl` and Lima's guest agents are bundled in the platform wheels — no separate install) that hosts many lightweight [Incus](https://linuxcontainers.org/incus/introduction/) containers, one per sandbox. The VM is sized to your full host RAM and CPU count with a 200 GiB sparse disk.
- **A host daemon** provides the `git`/`gh`/port-forward command bridge (over an SSH forced command bound to loopback) and idles containers and the VM back down when unused.
- **Shared caches across all sandboxes** keep repeat work fast: a pull-through container-registry cache (nginx), a shared BuildKit daemon (Docker layers cached across sandboxes), package caches for [Mise](https://mise.jdx.dev), cargo, npm/pnpm, pip/uv, go, and more, plus GitHub-release and k3s-installer caching.
- **btrfs with [bees](https://github.com/Zygo/bees) deduplication** for the container pool, so many similar sandboxes cost little disk. `node_modules` and `.venv` are redirected to the shared cache via a per-sandbox symlink (so opening a worktree on the host shows a symlink, not a real directory).
- **[Mise](https://mise.jdx.dev)** provides on-demand, version-managed tools inside each sandbox.

&nbsp;

## Uninstall

```sh
locki vm delete
uv tool uninstall locki
rm -rf ~/.local/share/locki ~/.config/locki
```

&nbsp;

## License

Copyright 2026 Jan Pokorný and [contributors](https://github.com/JanPokorny/locki/graphs/contributors)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
