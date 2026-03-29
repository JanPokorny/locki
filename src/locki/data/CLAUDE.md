You are running in a sandbox VM. This is an ephemeral environment designed to keep the main machine safe from malfunctioning agents. The folder is a fresh worktree: before delving into your task, start by setting up the environment. Check project metadata (`mise.toml`, `.tool-versions`, `.nvmrc`, `pyproject.toml`, etc.), CI definitions (`.github/workflows/*.yaml`, etc.) or docs (`README.md`, `CONTRIBUTING.md`, `*.md`, `docs/*`, etc.) to determine needed tools and their versions, and setup commands.

Do not use `git` or `gh` directly — they are not configured inside the sandbox. Instead, use the `locki` MCP server tool **`run_host_command`**, which runs allowed commands on the host where credentials and SSH keys live.

**Tool:** `run_host_command(worktree_path, exe, args)`
- `worktree_path`: your current working directory (`os.getcwd()` or `$PWD`)
- `exe`: `"git"` or `"gh"`
- `args`: list of arguments

**Allowed git subcommands:** `add`, `branch`, `checkout`, `commit`, `diff`, `fetch`, `log`, `push` (no `--force`), `restore`, `show`, `stash`, `status`, `tag`

**Allowed gh subcommands:** `pr create/view/list/diff/comment/review/status`, `issue create/view/list/comment`, `repo view`, `run view/list`, `workflow view/list`

**Example flow:**

```
run_host_command(worktree_path="/path/to/worktree", exe="git", args=["add", "-A"])
run_host_command(worktree_path="/path/to/worktree", exe="git", args=["commit", "-m", "feat: add a feature"])
run_host_command(worktree_path="/path/to/worktree", exe="git", args=["push"])
run_host_command(worktree_path="/path/to/worktree", exe="gh",  args=["pr", "create", "--title", "feat: add a feature", "--body", "..."])
```
