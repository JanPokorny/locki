<h1 align="center">🔒🐍 Locki</h1>
<p align="center">AI sandboxing for real-world projects</p>

&nbsp;

Locki is a CLI tool for Linux and macOS that allows running multiple AI agents in "yolo mode", without any interference or security risks.

&nbsp;

`$ locki codex my-new-feature` ← Codex opens in a fresh sandbox

`$ locki claude my-new-feature` ← Claude Code opens in a fresh sandbox

&nbsp;

**How is Locki different than other sandboxes?**
- _VM-based security_ -- Locki sandboxes are enclosed in a Lima VM. Nothing gets executed on host. Only raw code leaves the VM.
- _LXC-based environment_ -- Run anything: Python, Node.js, Docker or even full Kubernetes in the Locki sandboxes.
- _Worktree-based convenience_ -- Browse worktree files locally. See agent's changes natively in VSCode sidebar.

&nbsp;

**How to use?**

1. Install using your preferred manager:
    - `uv tool install locki`
    - `pipx install locki`
    - `mise use -g pipx:locki`
    - Host VM prerequisites are still required. On Ubuntu/Debian install QEMU with `sudo apt install qemu-system-x86 qemu-utils` (or the broader `qemu-system qemu-utils`). On macOS install it with `brew install qemu`.
2. `cd` to your Git repository and run one of:
    - `locki codex my-first-sandbox`
    - `locki claude my-first-sandbox`
    - `locki shell my-first-sandbox` for a regular shell session.
3. First start takes longer, wait a few minutes for the VM to boot.
4. Log in to your agent CLI inside the sandbox.
    - For Codex, run `codex login --device-auth` for ChatGPT login, or `printenv OPENAI_API_KEY | codex login --with-api-key` for API-key auth.
    - For Claude Code, follow the CLI login prompts.
    - Browser windows do not open automatically from the guest. Click the printed link or copy it manually.
    - Login state is persisted across sandboxes in `~/.locki/codex` and `~/.locki/claude`.
5. Build!
    - Agent is instructed to start by setting up project tools. This may take a bit of time. Subsequent sandbox installs will be much faster due to shared cache for most common dependency managers (`npm`, `pip`, etc.)
    - Want to configure a CLI manually? Copy config from `~/.claude` to `~/.locki/claude`, or from `~/.codex` to `~/.locki/codex`.
    - Locki does not auto-import host `~/.codex`; copy any desired config yourself.
6. Once happy, commit and push your changes from host. (Sandbox does not have Git access for security reasons.)
    - Locki ensures that Git hooks are still executed inside the sandbox.
    - **🚧 Upcoming feature**: MCP server exposing a safe subset of Git operations to the sandbox.
7. After merging the branch, remove the sandbox using: `locki remove my-first-sandbox`.
    - If you manually remove the worktree, Locki will eventually detect this and remove the sandbox too.

&nbsp;

**Smoke test on this repository**

1. `cd` to this repository.
2. Install the current checkout locally so the `locki` command exists:
    - `uv sync --dev`
3. Start a Codex sandbox for a throwaway branch:
    - `.venv/bin/locki codex smoke-hello`
4. When Codex opens, give it this prompt:

```text
Create a file named hello-locki.txt in the repository root containing exactly:
Hello world

Do not modify any other files.
```

5. On the host, find the worktree path:
    - `.venv/bin/locki list`
    - Look for the `smoke-hello` entry under `~/.locki/worktrees/...`
6. Verify the result on the host:
    - `cat ~/.locki/worktrees/<smoke-hello-worktree>/hello-locki.txt`
    - Expected contents: `Hello world`
7. Clean up when done:
    - `.venv/bin/locki remove smoke-hello --branch`

&nbsp;

**Connect To The VM From VS Code**

1. Install the VS Code `Remote - SSH` extension.
2. Start any Locki sandbox once so the Lima SSH config is generated:
    - `.venv/bin/locki shell smoke-hello -c "echo ready"` is enough.
3. Add this line to your host `~/.ssh/config`:
    - `Include ~/.locki/lima/locki/ssh.config`
4. In VS Code, run `Remote-SSH: Connect to Host...` and choose `lima-locki`.
5. Once connected, open `~/.locki/worktrees` in the VM and browse to the `smoke-hello` worktree.
6. This connection is to the shared Lima VM, not directly to the per-worktree Incus container.
    - Use `.venv/bin/locki shell <branch>` or `.venv/bin/locki codex <branch>` when you want to execute inside the sandbox container.

&nbsp;

**In trouble? Or need to uninstall Locki?** Run `locki factory-reset` to teardown the VM.

&nbsp;

**Notes on security:** Locki uses a single Lima VM which is set to only share the `~/.locki/worktrees` folder plus provider state in `~/.locki/claude` and `~/.locki/codex`. Each worktree has an associated LXC container (through Incus). LXC containers are not a security boundary (more so that Locki pokes holes in them for caching etc.), the shared VM is -- thus the only possible vector of escape is the source code written into a worktree. Codex is intentionally run with full access inside the container because the Lima VM and per-worktree Incus container provide the outer isolation boundary. In order to protect Git hook execution, Locki configures the worktree to use Locki-managed hooks that offload execution of parent repo hooks into the sandbox, and checks for `.git` file tampering. Despite best effort, Locki provides no security guarantees and is provided "as is".
