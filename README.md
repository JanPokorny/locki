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
  
</div>

&nbsp;

<b>Locki</b> is a CLI that safely runs AI agents with all permissions bypassed in isolated worktrees.

<table align=center>
<tr>
<th>❌ without Locki</th>
<th>✅ with Locki</th>
</tr>
<tr>
<td>

```sh
git worktree add -b fix-42 ../fix-42
cd ../fix-42
claude "fix issue #42"
# ...wait a few seconds
# ...approve a command
# ...wait a few seconds
# ...approve another command
# ...different agent rebuilt the image
#    and caused a name clash‽
# ...something is hogging the port‽
# ...approve another command
# ...
```

</td>
<td>

```sh
locki x claude "fix issue #42"
# ...go make a cup of tea
# ...drink tea 🍵
# ...look, the PR is ready
```

</td>
</tr>
</table>

&nbsp;

## Selling Points

- **First-class DX**: `locki ai`, work in your CLI of choice. Zero config. No sign Locki is even there.
- **No compromises**: Run anything including `systemd`, containers, even Kubernetes clusters.
- **Safe Git**: Sandboxes are only able to modify namespaced branches. Stash is scoped. Hooks are redirected.
- **Visibility and control**: Worktrees live on your computer, see and modify them right there.
- **Agent-friendly**: Bundled hand-picked tools and sandbox-specific instructions for best behavior.

Case study: [Kagenti ADK](https://github.com/kagenti/adk) uses Locki to run a full MicroShift node, allowing agents to verify their work using E2E tests on a real cluster. Something breaks? The agent can `kubectl` right in and debug, all contained within the Locki sandbox.

&nbsp;

## Tutorial

1. Install: `uv tool install locki`. ([Install uv](https://docs.astral.sh/uv/getting-started/installation/) first if you don't have it.)
1. If you're on Linux, also install [OpenSSH](https://repology.org/project/openssh/versions) (usually preinstalled) and [QEMU](https://www.qemu.org/download/#linux).
1. `cd` to your Git repository and run: `locki ai`

    <small>

    (Supported harnesses: `claude`, `gemini`, `codex`, `opencode`, `pi`.)

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

## Comparison

Most sandboxing solutions use one of these techniques:

- Full VM per sandbox: resource-heavy, slow to start
- MicroVM per sandbox: none or limited support for building, running and orchestrating containers
- OCI container per sandbox: none or limited support for building, running and orchestrating containers; potentially unsafe if runing VM-less on Linux
- OS-level jail (Landlock, Bubblewrap, etc.): just restriction, not isolation (ports collide, image tags get overwritten, etc.)

To my knowledge Locki is the only one packing a fully vertically integrated Incus-based solution. Seriously, stop reading this README and run `uvx locki ai`, that's all there is.

&nbsp;

## Pro-tips for power users

- Editors like VSCode show worktrees in the sidebar, useful as a quick UI for reviewing and modifying changes.\
  *(⚠️ VSCode 1.115.0+ requires setting `"git.detectWorktrees": true` for this to work.)*

- `locki list` shows worktree paths. `cd` to a worktree folder (`~/.local/share/locki/worktrees/...`) to operate on it directly. `locki` commands default to operating on the corresponding sandbox when in worktree folder.

- Working on two repos at once? `cd` into your sandbox's primary repo and run `locki include --repo ../other-repo` to graft the other repo into the current sandbox at `.locki/includes/<repo-name>/`. Or from the other repo: `locki include --this -m <sandbox-id>`.

- While `locki ai` opens a coding agent, `locki exec` (or short `locki x`) is the low-level version which can run any command. Pass a command to run in a sandbox, use `--match`/`-m` to select by branch substring or sandbox id: `locki exec -m big-refactor -- pytest`.

- The first `locki ai` run prompts you to pick a default harness; change it later in `~/.config/locki/config.toml` under `[ai] harness = "..."`.

- Ask your agent to forward ports, or use `locki port-forward` for more control.

- Locki sandboxes provide [Mise](https://mise.jdx.dev) for tool version management -- replacing `nvm`, `rbenv`, `brew` etc. with a single tool. Adding `mise.toml` to your repo with tool versions and task definitions will help agents and humans alike: ask your agent to do it!

- Want to use custom AI configuration in the VM -- instructions, skills, MCP servers, ...? Sandboxes share a home folder accessible at `~/.local/share/locki/home` on host (or `$XDG_DATA_HOME/locki/home`). For example, you can run `cp ~/.claude/CLAUDE.md ~/.local/share/locki/home/.claude/CLAUDE.md` to copy your custom instructions for use in sandboxes.

- Something is broken? Try `locki vm delete` -- it will preserve your worktrees and settings, but the VM and sandboxes will be recreated from scratch on next run.

- Sandboxes run on Fedora 43. Want a different OS? Create a `locki.toml` file in repo root referencing either [an available OS image](https://images.linuxcontainers.org/), or a local Incus rootfs tarball by path. Example:

  ```toml
  # locki.toml
  
  [incus_image]
  aarch64 = "ubuntu/questing"
  x86_64 = "ubuntu/questing"
  ```
  <small>(Since containers share a binary cache, it is not recommended to mix `musl` distros (like Alpine) with regular ones.)

&nbsp;

## Notes on security

Locki uses a single Lima VM which can only access the `~/.local/share/locki/worktrees` and `~/.local/share/locki/home` folders (honoring `$XDG_DATA_HOME`), which forms the security boundary. The sandboxed programs can read and write to these folders, and also access anything on the internet and local network. Furthermore, a guest-to-host SSH server exposes a limited set of `git` and `gh` subcommands, with write access restricted to the sandbox's own namespaced branches and stashes (so an agent in one sandbox cannot alter another sandbox's branch, the main branch, or unrelated stashes). `.git` files are checked for tampering when hooks are executed against them.

Locki is designed to provide protection for the host operating system and files from being messed up by a malfunctioning AI agent. There is no exfiltration protection, so be aware that API keys exposed to the agents need to be treated as potentially exposed and disposable, with limited scope. (This is no different from running the agent locally, just specifying that Locki does not help here. Use a dedicated solution like [OneCLI](https://github.com/onecli/onecli) if interested.)

Despite best effort, Locki provides no security guarantees and is provided "as is". That's the legal speak for "this is a random project by a random dude provided for free", you can't expect corporate-paid-support level security assurances. Random dude believes that while not perfect, using Locki is better than many existing sandboxing solutions and certainly better than going full `--yolo` on your bare machine and hoping for the best.

&nbsp;

## Tech

- Python CLI
- Single [Lima](https://lima-vm.io/) VM
- Multiple [Incus](https://linuxcontainers.org/incus/introduction/) containers
- [Mise](https://mise.jdx.dev) for ergonomic package installation
- Host proxy for self-service commands (`git`, `gh`, port forwarding)