# Sandbox environment

You are running inside a Locki sandbox -- an Incus LXC container running in a Lima VM. This environment is designed to give you free reign -- you are running as `root` -- while preventing accidental damage to files on the host machine.

You are operating on a separated worktree folder of a git repo lying outside of the sandbox -- for this reason, `.git` is just a file pointer and you can't access the actual `.git` folder. Git operations are only possible using the command bridge, see below.

The sandbox may also contain **included worktrees** from other repositories under `.locki/include/<repo-name>/`. Each include is a full git worktree of a separate repo; the command bridge rules apply inside each include the same way as in the main worktree (branch/stash ownership is scoped by the sandbox id, so commands work identically). `cd` into the include folder to operate on that repo. If the user asks you to work on multiple repos at once and an include is not yet present, tell the user to run `locki include --repo <path>` (or, from the other repo, `locki include --this -m <this-sandbox>`).

# Command bridge

Some commands execute on the host using a command bridge. This lets you execute a limited safe set of higher-priviledged commands. Run them as usual -- the executables present in sandbox are shims that call out to the bridge. The proxy will reject the command if it does not exactly match an allowed pattern. If user asks you to perform an operation you can't do, you can always prepare commands for them to run on host (worktree path matches 1:1).

## Git

```locki-bridged-command-filter
git add (--all | <file> ...)
git blame <file>
git branch (<name>#locki-<wt-id> [<start-point> | --move | --delete [--force]] | --show-current)
git check-ignore <file> ...
git cherry-pick (--continue | --abort | --skip)
git cherry-pick [--no-commit] [--gpg-sign] <ref>
git commit (-m/--message=<msg> [-s/--signoff] | -C/--reuse-message=<sha> | --amend --no-edit) [--amend [--no-edit]] [--gpg-sign]
git config [--get] [--local] <key>
git diff [--staged | --cached] [--name-only] [--stat] [--name-status] [<ref> [<ref>]] [<file> ...]
git fetch [--prune] [<ref>]
git grep [-l/--files-with-matches] <pattern> [<ref>] [<file> ...]
git log [--oneline] [--all] [--graph] [--reverse] [--format=<fmt>] [--pretty=<fmt>] [-n/--max-count=<n>] [--name-only] [--diff-filter=<filter>] [--since=<date>] [<ref>] [<file> ...]
git ls-files [<path> ...]
git merge (--continue | --abort | --skip)
git merge <ref>
git pull [--rebase] [--ff-only]
git push [--force-with-lease]
git rebase (--continue | --abort | --skip)
git rebase <ref>
git reflog
git reset [--hard] <ref>
git restore [--staged] [--source=<ref>] <file> ...
git rev-parse [--show-cdup] [--show-toplevel] [--git-dir] [--is-inside-work-tree] [--abbrev-ref] [--verify] [<arg> ...]
git show [<ref>] [--stat] [--name-only] [--name-status] [--format=<fmt>] [--pretty=<fmt>] [<file> ...]
git stash (pop | drop) <owned-stash-ref>
git stash apply <stash-ref>
git stash list
git stash push -m/--message=<text>#locki-<wt-id>
git status [-s/--short] [-u/--untracked-files=<mode>] [<file> ...]
git switch ([--create | --force-create] <name>#locki-<wt-id> [<start-point>] | --detach <ref>)
```

`<wt-id>` is the 8-char slug in worktree directory name: `.../locki/worktrees/<repo-name>#locki-<wt-id>`. Branches you create, modify, or switch to must be named matching this pattern: `<name>#locki-<wt-id>`. You may read from any ref. `<owned-stash-ref>` is a stash whose message contains `#locki-<wt-id>` -- only those can be popped or dropped; any stash can be applied.

### Interactive rebase

`git rebase --interactive` is unavailable -- replay commits by hand instead.

Setup:

  git branch backup#locki-<wt-id>
  git log --reverse --oneline <upstream>..HEAD
  git switch --detach <new-base>

Per SHA:
- pick = `git cherry-pick <sha>` (on conflict: resolve, `git add .`, `git cherry-pick --continue`)
- reword/edit = pick, make changes, amend
- squash/fixup = `git cherry-pick --no-commit <sha>`, amend

Finish:

  git switch --force-create <original-branch>#locki-<wt-id>
  git diff backup#locki-<wt-id>..HEAD
  git branch backup#locki-<wt-id> --delete --force

## GitHub CLI

```locki-bridged-command-filter
gh api repos/<owner>/<repo>/pulls/<number>/comments
gh issue (view [<number>] [--comments] | list [-L/--limit=<n>] [-s/--state=<state>] [-S/--search=<query>])
gh pr (view [<number>] [--comments] [--json=<fields>] | list [-L/--limit=<n>] [-s/--state=<state>] [-S/--search=<query>] | diff [<number>] [--name-only] [--patch] [--stat] | status | checks [<number>])
gh pr comment <number> -b/--body=<b>
gh pr create -t/--title=<t> [-b/--body=<b>] [-B/--base=<b>] [-H/--head=<h>] [-d/--draft] [-f/--fill] [-r/--reviewer=<r>] [-l/--label=<l>] [-a/--assignee=<a>]
gh pr edit [<number>] [-t/--title=<t>] [-b/--body=<b>] [--add-label=<l>] [--add-reviewer=<r>] [--add-assignee=<a>]
gh run (view [<number>] [-j/--job=<number>] [--log] [--log-failed] | list [-L/--limit=<n>])
```

`<owner>/<repo>` may only be the current repo.

## Port forwarding

```locki-bridged-command-filter
locki port-forward :<number> ...
```

When you start a network service the user should access, forward the port to host. Host port will be picked automatically and shown in stderr output. Give the user a full URL with the host port, e.g. `http://localhost:<host_port>`.

## Web browser

You can open, inspect and interact with websites using agent-browser. When in need of a browser, start by running `agent-browser --help`. If it gives you trouble, run `agent-browser doctor` to autofix known issues.

## Other

Useful tools preinstalled: `docker`, `fd`, `rg`, `yq`, `kubectl`, `pnpm`, `uv`.

# Startup checklist

Perform always when starting a conversation.

1. Check project metadata (`mise.toml`, `.tool-versions`, `.nvmrc`, `pyproject.toml`, etc.), CI definitions (`.github/workflows/*.yaml`, etc.) or docs (`README.md`, `CONTRIBUTING.md`, `*.md`, `docs/*`, etc.) to determine needed tools and their versions, and setup commands. If there is `mise.toml`, run `mise install` to set up all tools. Otherwise manually enable specific tool versions using e.g.: `mise use -g python@3.12.1`, `mise use -g node@22`, `mise use -g jq`, falling back to OS package manager if `mise` does not have the tool (`dnf` by default, unless running on a custom image).

2. Check current branch name using `git branch --show-current`. If it is `untitled#locki-<wt-id>`, reset it to main using `git reset --hard main`, then rename using `git branch <new-name>#locki-<wt-id> --move`. Pick `<new-name>` based on the task at hand.

# Cleanup checklist

Perform when user asks you to cleanup the sandbox. This may come at the end, or even beginning of conversation, if the user forgot to cleanup before starting a new conversation.

1. Check current branch name using `git branch --show-current`. If it is NOT `untitled#locki-<wt-id>`, run `git switch --force-create untitled#locki-<wt-id> origin/main`.

2. If the user assigned you more work after the cleanup, continue by following the startup checklist.
