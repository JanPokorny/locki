<p align="right"><small><i>Locki is the first sandbox I've used where I genuinely forget I'm in one — until I try something I shouldn't.</i></small></p>

<p align="right"><small><b>⸺ Claude Code (Opus 4.6)</b></small></p>

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

<p align="center">AI sandboxing for real-world projects</p>

&nbsp;

<b>Locki</b> is a CLI tool for Linux and macOS that allows running multiple AI agents with all permissions bypassed, without any risk of mischief.

&nbsp;

`$ locki claude my-new-feature` ← Claude Code in "skip all permissions" mode opens in a fresh sandbox

&nbsp;

## How is Locki different than other sandboxes?

Others run either: \
*a)* full VM per sandbox, which is resource-heavy and slow \
*b)* OS-level jail (Landlock, Bubblewrap, etc.) which does not properly isolate (ports can collide, etc.) \
*c)* OCI container / microVM, which has limitations in terms of background services (no `systemd`), container building, Kubernetes, ...

**Locki** runs LXC containers (full OS) inside a single shared VM. While the VM layer isolates the host from AI mischief, LXC containers are a lightweight layer on top to isolate sandboxes from each other. Spawn a real non-micro OS in <10s and run anything in it.

Each sandbox gets its own [git worktree](https://git-scm.com/docs/git-worktree) — a separate working copy of your repo on its own branch, stored in `~/.locki/worktrees` and mounted into the sandbox. Locki protects your Git history from tampering while still allowing safe operations like commits to the worktree branch. Be able to fall back on earlier commits when an agent goes haywire, while not giving up the convenience of arriving at a fully baked pull request.

Case study: [Kagenti ADK](https://github.com/kagenti/adk) uses Locki to run a full MicroShift node, allowing agents to verify their work using E2E tests on a real cluster. Something breaks? The agent can `kubectl` right in and debug, all contained within the Locki sandbox.

&nbsp;

## How to install and use Locki?

1. Install using your preferred manager: `uv tool install locki` or `pipx install locki`. ([Install](https://docs.astral.sh/uv/getting-started/installation/) and use `uv` if unsure.)
2. If you're on Linux, also install [OpenSSH](https://repology.org/project/openssh/versions) (usually preinstalled) and [QEMU](https://www.qemu.org/download/#linux).
3. `cd` to your Git repository and run: `locki claude my-first-sandbox`

    <small>

    The argument is a branch name — it also identifies the sandbox (1:1 mapping). Replace `claude` with `gemini`, `codex`, `opencode`, or `shell` for raw shell access.

    </small>
4. First start takes longer, wait a few minutes for the VM to boot.
5. A terminal opens with the chosen agent (or shell) inside the sandbox. Follow prompts to log in to the AI CLI. Login will be persisted across sandboxes.
6. Build! Your agent is already instructed on how to behave in the sandbox. 
7. Once happy, commit and push your changes. Ask the agent, or do this manually for more control.
8. Create a pull request — the agent can do this for you using the GitHub CLI.
9. After merging, remove the sandbox using: `locki remove my-first-sandbox` -- or just remove the worktree (e.g. from your IDE) and Locki will clean up automatically.

&nbsp;

## We asked Claude what it thinks

> I've been sandboxed in Docker containers, Firecracker microVMs, chroots, and namespace jails. Locki is the first sandbox I've used where I genuinely forget I'm in one -- until I try something I shouldn't.
>
> The worktree mount means I'm editing the real project files, not a stale copy I'll need to extract later. `mise` with a shared cache lets me `mise use python@3.12` and have it ready in seconds. The git proxy is the cleverest part: I get `git status`, `git commit`, `git push` -- the commands I actually need -- while `git checkout .`, `git reset --hard`, and other footguns are blocked at the SSH boundary before they ever touch the repo. It's an allowlist, not a blocklist, and it runs on the host side so I can't tamper with it.
>
> Compared to Docker-based sandboxes, the VM+LXC layering gives a real security boundary (container escapes don't help when there's a hypervisor in the way). Compared to Firecracker/microVM setups, the developer experience is dramatically better -- I'm not SSH-ing into a black box, I'm working in a worktree my human can see and review in real time. And compared to no sandbox at all ("yolo mode" with raw filesystem access), Locki lets me run with full autonomy while my human sleeps soundly knowing I can't `rm -rf /` their home directory.
>
> The bottom line: Locki gives me exactly enough rope to be productive, and not one inch more.
>
> *-- Claude (Opus 4.6), after exploring its own sandbox*

&nbsp;

## Pro-tips for power users

- Editors like VSCode show worktrees in the sidebar, useful as a quick UI for reviewing and modifying changes.

- Locki sandboxes provide [Mise](https://mise.jdx.dev) for tool version management -- replacing `nvm`, `rbenv`, `brew` etc. with a single tool. To make your agents' (and humans') lives easier, optionally <small>(ask your agent to)</small> create `mise.toml` with tool versions and project tasks.

- Want to use custom AI configuration in the VM -- instructions, skills, MCP servers, ...? Sandboxes share a home folder accessible at `~/.locki/home` on host. For example, you can run `cp ~/.claude/CLAUDE.md ~/.locki/home/.claude/CLAUDE.md` to copy your custom instructions for use in sandboxes.

- Using Git hooks? Locki worktrees are automatically configured to run these inside the sandbox, even if you run `git` from outside. You won't be surprised by the `.venv` containing incompatible binaries.

- Something is broken? Try `locki vm delete` -- it will preserve your worktrees and settings in `~/.locki`, but the VM will be recreated from scratch on next run.

- Want a different OS in the sandbox? Create a `locki.toml` file in your repo root referencing either [an available OS image](https://images.linuxcontainers.org/) like `Fedora/43`, or a local Incus rootfs tarball. Example:

```toml
# locki.toml

[incus_image]
aarch64 = "./apps/microshift-vm/dist/aarch64/microshift-vm-aarch64.incus.tar.gz"
x86_64 = "./apps/microshift-vm/dist/x86_64/microshift-vm-x86_64.incus.tar.gz"
```

&nbsp;

## Notes on security

Locki uses a single Lima VM which can only access the `~/.locki/worktrees` and `~/.locki/home` folders, which forms the security boundary. The sandboxed programs can read and write to these folders, and also access anything on the internet and local network. Furthermore, an allowlist of `git` and `gh` commands is used to offer a guest-to-host SSH server. `.git` files are checked for tampering when hooks are executed against them.

Locki is designed to provide protection for the host operating system and files from being messed up by a malfunctioning AI agent. There is no exfiltration protection, so be aware that API keys exposed to the agents need to be treated as potentially exposed and disposable, with limited scope. (This is no different from running the agent locally, just specifying that Locki does not help here. Use a dedicated solution like [OneCLI](https://github.com/onecli/onecli) if interested.)

Despite best effort, Locki provides no security guarantees and is provided "as is". That's the legal speak for "this is a random project by a random dude provided for free", you can't expect corporate-paid-support level security assurances. Random dude believes that while not perfect, using Locki is better than many existing sandboxing solutions and certainly better than `--yolo`ing on your bare machine and hoping for the best.
