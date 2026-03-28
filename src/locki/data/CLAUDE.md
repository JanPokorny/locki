You are running in a sandbox VM. This is an ephemeral environment designed to keep the main machine safe from malfunctioning agents. The folder is a fresh worktree: before delving into your task, start by setting up the environment. Check project metadata (`mise.toml`, `.tool-versions`, `.nvmrc`, `pyproject.toml`, etc.), CI definitions (`.github/workflows/*.yaml`, etc.) or docs (`README.md`, `CONTRIBUTING.md`, `*.md`, `docs/*`, etc.) to determine needed tools and their versions, and setup commands.

Do not use `git` or `gh` directly — they are not configured inside the sandbox. Instead, use the `locki` MCP server, which runs on the host and has access to all credentials. Your current working directory is your worktree path; pass it to every tool call.

Available MCP tools (server name: `locki`):

- **`git_commit(worktree_path, message)`** — stages all changes and commits
- **`git_push(worktree_path, remote="origin")`** — pushes the current branch
- **`gh_pr_create(worktree_path, title, body="", base="main")`** — opens a GitHub PR

Example flow once your changes are ready:

1. `git_commit(worktree_path=os.getcwd(), message="feat: add a feature")`
2. `git_push(worktree_path=os.getcwd())`
3. `gh_pr_create(worktree_path=os.getcwd(), title="feat: add a feature", body="...")`
