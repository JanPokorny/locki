<p align="right"><small><i>Locki is the first sandbox I've used where I genuinely forget I'm in one — until I try something I shouldn't.</i></small></p>

<p align="right"><small><b>⸺ Claude Code</b></small></p>

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

<p align="center">AI sandboxing without the taste of sand</p>

<div align=center>
  
![](https://badgen.net/badge/_/macOS%20%E2%9C%94/green?icon=apple&label)
![](https://badgen.net/badge/_/Linux%20%E2%9C%94/green?icon=linux&label)
![](https://badgen.net/badge/_/uv%20tool%20install%20locki/DE5FE9?icon=uv&label)
[![](https://img.shields.io/pypi/v/locki?color=DE5FE9&label=PyPI)](https://pypi.org/project/locki/)
![](https://img.shields.io/pypi/l/locki?color=green&label=License)
  
</div>

&nbsp;

**Locki** is an AI sandbox for real-world projects. Not another vibe-coded `docker` wrapper, but an actual engineered solution built for the development needs of [Agent Stack](https://github.com/i-am-bee/agentstack). Other sandboxes break down on anything more complex than "a single Next.js app". Locki can handle as many containers, systemd services and even Kubernetes clusters as you like (and your RAM allows).

&nbsp;

<table align=center>
<tr>
<td colspan=3 align=center>

Run agents in parallel with no collisions in ports, image tags, or infrastructure:

</td>
<tr>
<tr>
<td>

```sh
locki x claude "fix #42"
# Read issue using `gh`...
# Determined the cause...
# Fixed in code...
# Built image app:local...
# Started k3s cluster...
# Verified the fix...
```

</td>
<td>

```sh
locki x codex "improve perf"
# Built image app:local...
# Started k3s cluster...
# Measured performance...
# Improved critical paths...
# Re-built and re-deployed...
# Measured again...
```

</td>
<td>

```sh
locki x pi "add CZ translation"
# Built image app:local...
# Started k3s cluster...
# Explored UI in context...
# Translated strings...
# Re-built and re-deployed...
# Verified accuracy...
```

</td>
</tr>
</table>

&nbsp;

- **First-class DX**: Work with your AI of choice. Zero config. No sign Locki is even there.
- **No compromises**: Run anything including `systemd`, containers, even Kubernetes clusters.
- **Worktree-backed**: All data remains on your disk in a git worktree. No need to dig in VMs.
- **Safe Git**: Agents are only able to modify namespaced branches. Stash is scoped. Hooks are redirected.
- **Visibility and control**: Worktrees live on your computer, see and modify them right there.
- **Agent-friendly**: Bundled hand-picked tools and sandbox-specific instructions for best behavior.

Case study: [Kagenti ADK](https://github.com/kagenti/adk) uses Locki to run a full MicroShift node, allowing agents to verify their work using E2E tests on a real cluster. Something breaks? The agent can `kubectl` right in and debug, all contained within the Locki sandbox.

&nbsp;

## Tutorial

1. Install: `uv tool install locki`. ([Install uv](https://docs.astral.sh/uv/getting-started/installation/) first if you don't have it.)
1. If on Linux, install [QEMU](https://www.qemu.org/download/#linux). (Not needed on macOS.)
1. `cd` to your Git repository and run: `locki ai`

    <small>

    (Supported harnesses: `claude`, `gemini`, `codex`, `opencode`, `pi`, `copilot`.)

    </small>
1. First start takes longer, wait a few minutes for the VM to boot.
1. Follow prompts to log in to the AI CLI. Login will be persisted across sandboxes.
1. Build! Your agent is already instructed on how to behave in the sandbox.
1. Run `locki ai` again to open an interactive selector: continue existing session, or start a new one.
1. Once happy, commit and push your changes. Ask the agent, or do this manually for more control.
1. After merging the branch, just delete the worktree from your IDE and Locki will clean up the sandbox.

    <small>

    (Or do it manually with: `locki remove`)

    </small>

&nbsp;

## Folders

Each sandbox gets its own [worktree](https://git-scm.com/docs/git-worktree) (a full copy of your repo) and shares a common home folder. The original repo and your actual home stay safely out of reach:

- **Your Git repo** (`~/myproject/`) — ❌ Not visible from any sandbox. That means sandboxes can't reach the `.git` folder and mess it up -- all `git` calls go through a command bridge and get reviewed and filtered.
- **Each sandbox's worktree** (`~/.local/share/locki/worktrees/myproject-locki-.../`) — Visible in corresponding sandbox, at the same path. All edits in the sandbox are instantly visible on host.
- **Shared sandbox home** (`~/.local/share/locki/home/`) — Visible from every sandbox as `~`. Save your agent configuration here to use it in sandboxes.
- **Your actual home** (`~`) — ❌ Not visible from any sandbox. Sandboxes can't mess up your global config.

&nbsp;

## Commands

Every command supports `-h`/`--help`; most data commands take `--json` for scripting. Sandbox-selecting commands share `-m/--match` (id prefix or branch substring), `-i/--interactive` (force the picker), and `-n/--new` (create fresh).

| Command | What it does |
| --- | --- |
| `locki ai` | Start your AI harness in a sandbox — resume, pick, or create. |
| `locki exec` \| `x` `-- <cmd>` | Run any command in a sandbox (default: `bash`). |
| `locki ide` | Open your editor on the sandbox worktree (runs on host). |
| `locki new` \| `n` | Create a sandbox worktree without entering it. |
| `locki list` \| `ls` `[--all]` | List sandboxes for the current repo (or `--all` repos). |
| `locki include` `(--repo <path> \| --this)` | Graft another repo's worktree into a sandbox. |
| `locki port-forward` \| `pf` `[--list] [--clear] [port[:port]] ...` | Forward host ports to a sandbox. |
| `locki remove` \| `rm` \| `delete` `[-f] [-b] [--merged]` | Remove a sandbox (`-b` also deletes its branches; `--merged` sweeps merged-and-clean ones). |
| `locki setup` `[--defaults] [--copy]` | Setup wizard: pick harness/editor, copy AI config into the sandbox home. |
| `locki vm` `status`\|`st` / `stop` / `prune` / `delete` | Manage the shared Lima VM and its caches. |

&nbsp;

## Comparison

Most sandboxing solutions use one of these techniques:

- **Full VM per sandbox** (Vagrant, Multipass): resource-heavy, slow to start
- **MicroVM per sandbox** (Firecracker, Apple `container`): none or limited support for building, running and orchestrating containers
- **OCI container per sandbox** (Devcontainers, Distrobox, `container-use`): none or limited support for building, running and orchestrating containers; potentially unsafe if running VM-less on Linux
- **OS-level jail** (Landlock, Bubblewrap, `sandbox-exec`): just restriction, not isolation (ports collide, image tags get overwritten, etc.)

Locki instead runs **one Lima VM hosting many lightweight Incus containers** — one shared kernel boundary you can trust, cheap per-sandbox containers, and full support for building and orchestrating containers (even Kubernetes) inside. Seriously, stop reading this README and run `uvx locki ai`, that's all there is.

&nbsp;

## Pro-tips for power users

- Launch an IDE in the worktree folder using `locki ide`. 30+ editors are recognized out of the box (VSCode, Cursor, Zed, the JetBrains suite, Neovim, Sublime Text, ...), and you can set any custom command via `locki setup` or `ide_command`.\
  *(The IDE runs on host: you still need to run `locki ai` / `locki x -- <cmd>` in the built-in terminal to run commands in the sandbox. This is intentional: running your IDE inside the sandbox (using "remote SSH" or similar features) is unsafe, since the agent could potentially access authentication tokens stored in the IDE's memory.)*

- When `cd`'d into a worktree folder (`~/.local/share/locki/worktrees/.../`), `locki` commands use it by default -- otherwise they show an interactive picker. Use `--match`/`-m` to select by sandbox id or branch substring. `locki list` (alias `ls`) shows every sandbox and its worktree path.

- Editors like VSCode show worktrees in the sidebar, useful as a quick UI for reviewing and modifying changes.\
  *(⚠️ VSCode 1.115.0+ requires setting `"git.detectWorktrees": true` for this to work.)*

- Working on two repos at once? `cd` into your sandbox's primary repo and run `locki include --repo ../other-repo` to graft the other repo into the current sandbox at `.locki/include/<repo-name>-locki-<sandbox-id>/`. Or from the other repo: `locki include --this -m <sandbox-id>`.

- While `locki ai` opens a coding agent, `locki exec` (or short `locki x`) is the low-level version which can run any command. Pass a command to run in a sandbox, use `--match`/`-m` to select by branch substring or sandbox id: `locki exec -m big-refactor -- pytest`.

- The first `locki ai` run prompts you to pick a default harness and editor. Re-run `locki setup` to change them, or edit `~/.config/locki/config.toml` directly — the keys are full command lines, e.g. `ai_command = "gemini --yolo -r"` and `ide_command = "code ."`. A repo can override `ai_command` via a `locki.toml` in its root; `ide_command` is user-only (it launches on your host).

- Ask your agent to forward ports, or use `locki port-forward` for more control.

- Locki sandboxes provide [Mise](https://mise.jdx.dev) for tool version management -- replacing `nvm`, `rbenv`, `brew` etc. with a single tool. Adding `mise.toml` to your repo with tool versions and task definitions will help agents and humans alike: ask your agent to do it!

- Want to use custom AI configuration in the VM -- instructions, skills, MCP servers, ...? Sandboxes share a home folder accessible at `~/.local/share/locki/home` on host (or `$XDG_DATA_HOME/locki/home`). For example, you can run `cp ~/.claude/CLAUDE.md ~/.local/share/locki/home/.claude/CLAUDE.md` to copy your custom instructions for use in sandboxes.

- Something is broken? Try `locki vm delete` -- it will preserve your worktrees and settings, but the VM and sandboxes will be recreated from scratch on next run.

- Sandboxes run on Fedora 43. Want a different OS? Create a `locki.toml` file in repo root referencing either [an available OS image](https://images.linuxcontainers.org/), or a local Incus image archive by path. For the local archive format, see the [Incus image format documentation](https://linuxcontainers.org/incus/docs/main/reference/image_format/). Example:

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

## Notes on security

Locki uses a single Lima VM which can only access the `~/.local/share/locki/worktrees` and `~/.local/share/locki/home` folders (honoring `$XDG_DATA_HOME`), which forms the security boundary. The sandboxed programs can read and write to these folders, and also access anything on the internet and local network. Furthermore, a guest-to-host SSH server exposes a limited set of `git` and `gh` subcommands, with write access restricted to the sandbox's own namespaced branches and stashes (so an agent in one sandbox cannot alter another sandbox's branch, the main branch, or unrelated stashes). `.git` files are checked for tampering when hooks are executed against them.

Locki is designed to provide protection for the host operating system and files from being messed up by a malfunctioning AI agent. There is no exfiltration protection, so be aware that API keys exposed to the agents need to be treated as potentially exposed and disposable, with limited scope. (This is no different from running the agent locally, just specifying that Locki does not help here. Use a dedicated solution like [DAM!](https://github.com/dam-agents/dam) if interested.)

Locki may not provide perfect security, however we believe it works better than many existing sandboxing solutions and certainly better than going full `--yolo` on your bare machine and hoping for the best.

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
locki vm delete            # stop and remove the Lima VM
uv tool uninstall locki    # remove the CLI
rm -rf ~/.local/share/locki ~/.config/locki   # worktrees, shared home, config (honors $XDG_*)
```

&nbsp;

## Troubleshooting

- **Something is wedged?** `locki vm delete` recreates the VM from scratch on the next run; worktrees and settings are preserved.
- **`Command bridge proxy is disabled`** — the host daemon didn't come up in time; re-run the command, and check `~/.local/state/locki/logs/daemon.log`.
- **On Linux, "requires QEMU"** — install [QEMU](https://www.qemu.org/download/#linux) (`qemu-system-<arch>` + `qemu-img`).
- **VSCode worktree sidebar empty** — set `"git.detectWorktrees": true` (needed on VSCode 1.115.0+).

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
