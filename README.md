<p align="center">
    <h1>🔒🐍 Locki</h1>
    AI sandboxing for real-world projects
</p>

&nbsp;

Locki is a CLI tool for Linux and macOS that allows running multiple AI agents in "yolo mode", without any interference or security risks.

&nbsp;

```bash
locki claude my-new-feature   # <- Claude CLI in "skip all permission" mode opens in a fresh sandbox
```

&nbsp;

**How is Locki different than other sandboxes?**
- _VM-based security_ -- Locki sandboxes are enclosed in a Lima VM. Nothing gets executed on host. Only raw code leaves the VM.
- _LXC-based environment_ -- Run anything, like Docker and even full Kubernetes in the Locki sandboxes.
- _Worktree-based convenience_ -- Browse worktree files locally. See agent's changes natively in VSCode sidebar.

&nbsp;

**How to use?**

1. Install using your preferred manager:
    - `uv tool install locki`
    - `pipx install locki`
    - `mise use -g pipx:locki`
2. `cd` to your Git repository and run: `locki claude my-first-sandbox`
    - Replace `claude` with `shell` to open a regular shell session.
3. First start takes longer, wait a few minutes for the VM to boot.
4. Follow prompts to log in to Claude Code CLI.
    - Browser won't open. Click the link or press `c` to copy instead.
    - Login will be persisted across sandboxes.
5. Build!
    - Agent is instructed to start by setting up project tools. This may take a bit of time. Subsequent sandbox installs will be much faster due to shared cache for most common dependency managers (`npm`, `pip`, etc.)
    - Want to configure Claude Code CLI? Copy your config from `~/.claude` to `~/.locki/claude` to use it in the VM!
6. Once happy, commit and push your changes from host. (Sandbox does not have Git access for security reasons.)
    - Locki ensures that Git hooks are still executed inside the sandbox.
    - **🚧 Upcoming feature**: MCP server exposing a safe subset of Git operations to the sandbox.
7. After merging the branch, remove the sandbox using: `locki remove my-first-sandbox`.
    - If you manually remove the worktree, Locki will eventually detect this and remove the sandbox too.

&nbsp;

**In trouble? Or need to uninstall Locki?** Run `locki factory-reset` to teardown the VM.

&nbsp;

**Notes on security:** Locki uses a single Lima VM which is set to only share the `~/.locki/worktrees` folder. Each worktree has an associated LXC container (through Incus). LXC containers are not a security boundary (more so that Locki pokes holes in them for caching etc.), the shared VM is -- thus the only possible vector of escape is the source code written into a worktree. In order to protect Git hook execution, Locki configures the worktree to use Locki-managed hooks that offload execution of parent repo hooks into the sandbox, and checks for `.git` file tampering. Despite best effort, Locki provides no security guarantees and is provided "as is".