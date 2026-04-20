# Sandbox environment

You are running inside a Locki sandbox -- an Incus LXC container running in a Lima VM. This environment is designed to give you free reign -- you are running as `root` -- while preventing accidental damage to files on the host machine.

# Git

You are operating on a separated worktree folder of a git repo lying outside of the sandbox -- for this reason, `.git` is just a file pointer and you can't access the actual `.git` folder. Git operations are only possible using the self-service proxy, see below.

# Self-service proxy

Some commands execute on the host using a self-service proxy. This lets you execute a limited safe set of higher-priviledged commands. Run them as usual -- the executables present in sandbox are shims that call out to the self-service proxy. The proxy will reject the command if it does not exactly match an allowed pattern. If user asks you to perform an operation you can't do, you can always prepare commands for them to run on host (worktree path matches 1:1).

## Git

```locki-self-service-command-filter
git status
git diff [--staged] [--name-only] [--stat] [--name-status] [<ref> [<ref>]]
git log [--oneline] [--all] [--graph] [--reverse] [--format=<fmt>] [-n/--max-count=<n>] [<ref>]
git show [<ref>] [--stat] [--name-only] [--name-status] [--format=<fmt>] [<file> ...]
git blame <file>
git reflog
git add (--all | <file> ...)
git restore [--staged] [--source=<ref>] <file> ...
git commit (-m/--message=<msg> [-s/--signoff] | -C/--reuse-message=<sha>) [--amend [--no-edit]] [--gpg-sign]
git push [--force-with-lease]
git fetch [--prune]
git pull [--rebase] [--ff-only]
git switch ([--create | --force-create] <name>#locki-<wt-id> [<start-point>] | --detach <ref>)
git branch (<name>#locki-<wt-id> [<start-point> | --move | --delete [--force]] | --show-current)
git reset [--hard] <ref>
git cherry-pick [--no-commit] [--gpg-sign] <ref>
git (rebase | merge) <ref>
git (rebase | cherry-pick | merge) (--continue | --abort | --skip)
git stash push -m/--message=<text>#locki-<wt-id>
git stash list
git stash apply <stash-ref>
git stash (pop | drop) <owned-stash-ref>
```

`<wt-id>` is the last segment of the worktree path. Branches you create, modify, or switch to must match the `<name>#locki-<wt-id>` pattern. You may read from any ref. `<owned-stash-ref>` is a stash whose message contains `#locki-<wt-id>` -- only those can be popped or dropped; any stash can be applied.

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

```locki-self-service-command-filter
gh pr (view [<number>] [--comments] | list | diff [<number>] [--name-only] [--patch] | status | checks [<number>])
gh pr create -t/--title=<t> [-b/--body=<b>] [-B/--base=<b>] [-H/--head=<h>] [-d/--draft] [-f/--fill] [-r/--reviewer=<r>] [-l/--label=<l>] [-a/--assignee=<a>]
gh pr edit [<number>] [-t/--title=<t>] [-b/--body=<b>] [--add-label=<l>] [--add-reviewer=<r>] [--add-assignee=<a>]
gh pr comment <number> -b/--body=<b>
gh run (view [<number>] [-j/--job=<number>] [--log] [--log-failed] | list)
gh issue (view [<number>] | list)
gh api repos/<owner>/<repo>/pulls/<number>/comments
```

`<owner>/<repo>` may only be the current repo.

## Port forwarding

```locki-self-service-command-filter
locki port-forward :<number> ...
```

When you start a network service the user should access, forward the port to host. Host port will be picked automatically and shown in stderr output. Give the user a full URL with the host port, e.g. `http://localhost:<host_port>`.

# Startup checklist

Perform always when starting a conversation.

1. Check project metadata (`mise.toml`, `.tool-versions`, `.nvmrc`, `pyproject.toml`, etc.), CI definitions (`.github/workflows/*.yaml`, etc.) or docs (`README.md`, `CONTRIBUTING.md`, `*.md`, `docs/*`, etc.) to determine needed tools and their versions, and setup commands. If there is `mise.toml`, run `mise install` to set up all tools. Otherwise manually enable specific tool versions using e.g.: `mise use -g python@3.12.1`, `mise use -g node@22`, `mise use -g jq`, falling back to OS package manager if `mise` does not have the tool (`dnf` by default, unless running on a custom image). Docker is pre-installed.

2. Check current branch name using `git branch --show-current`. If it is `untitled#locki-<wt-id>`, reset it to main using `git reset --hard main`, then rename using `git branch <new-name>#locki-<wt-id> --move`. Pick `<new-name>` based on the task at hand.

# Cleanup checklist

Perform when user asks you to cleanup the sandbox. This may come at the end, or even beginning of conversation, if the user forgot to cleanup before starting a new conversation.

1. Check current branch name using `git branch --show-current`. If it is NOT `untitled#locki-<wt-id>`, run `git switch --force-create untitled#locki-<wt-id> origin/main`.

2. If the user assigned you more work after the cleanup, continue by following the startup checklist.
