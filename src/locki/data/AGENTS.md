# Sandbox environment

You are running inside a Locki sandbox -- an Incus container running in a Lima VM. This environment is designed to give you free rein -- you are running as `root` -- while preventing accidental damage to files on the host machine.

You are operating on a separated worktree folder of a git repo lying outside of the sandbox -- for this reason, `.git` is just a file pointer and you can't access the actual `.git` folder. Git operations are only possible using the command bridge, see below.

The `.locki/tmp/` folder, like the rest of the worktree, is shared with the host -- the user can see its contents. Put screenshots (e.g. from agent-browser), scratch files, debug dumps, and other artifacts there.

The sandbox may also contain **included worktrees** from other repositories under `.locki/include/<repo-name>-locki-<wt-id>/`. Each include is a full git worktree of a separate repo; the command bridge rules apply inside each include the same way as in the main worktree (branch/stash ownership is scoped by the sandbox id, so commands work identically). `cd` into the include folder to operate on that repo. If the user asks you to work on multiple repos at once and an include is not yet present, tell the user to run `locki include --repo <path>` (or, from the other repo, `locki include --this -m <this-sandbox>`).

# Command bridge

Some commands execute on the host using a command bridge. This lets you execute a limited safe set of higher-privileged commands. Run them as usual -- the executables present in sandbox are shims that call out to the bridge. The proxy will reject the command if it does not exactly match an allowed pattern. If user asks you to perform an operation you can't do, you can always prepare commands for them to run on host (worktree path matches 1:1).

## Git

```locki-bridged-command-filter
git -v/--version
git add (--all | <file> ...)
git blame [-L=<range>] <file>
git branch (<name>#locki-<wt-id> [<start-point> | --move | --delete [--force]] | --show-current | [-l/--list] [-a/--all | -r/--remotes] [-v/--verbose] [--contains <ref>] [--merged [<ref>]] [--no-merged [<ref>]] [<pattern>])
git cat-file -t/--type <ref>
git checkout (--ours | --theirs) <file> ...
git check-ignore <file> ...
git cherry-pick (--continue | --abort | --skip)
git cherry-pick [--no-commit] [--gpg-sign] <ref> ...
git commit (-m/--message=<msg> [-s/--signoff] | -C/--reuse-message=<sha> [-s/--signoff] | --amend --no-edit [-s/--signoff]) [--amend [--no-edit]] [--allow-empty] [--gpg-sign]
git config [--get] [--local] <key>
git count-objects [-v/--verbose]
git describe [--tags] [--always] [--abbrev=<n>] [<ref>]
git diff [--staged | --cached] [--name-only] [--stat] [--numstat] [--name-status] [--exit-code] [--diff-filter=<filter>] [-U/--unified=<n>] [-w/--ignore-all-space] [--ignore-space-change] [<ref> [<ref>]] [<file> ...]
git fetch [--prune] [<remote> [<ref> ...]]
git for-each-ref [--format=<fmt>] [--sort=<key>] [<pattern> ...]
git grep [-l/--files-with-matches] <pattern> [<ref>] [<file> ...]
git hash-object <file>
git log [--oneline] [--all] [--graph] [--reverse] [--first-parent] [--stat] [-p/--patch] [--follow] [--no-merges] [--merges] [--decorate] [--left-right] [-S=<string>] [-G=<regex>] [--format=<fmt>] [--pretty=<fmt>] [-n/--max-count=<n>] [--name-only] [--name-status] [--diff-filter=<filter>] [--since=<date>] [--grep=<pattern>] [--author=<author>] [--ancestry-path] [--not] [<ref>] [<file> ...]
git ls-files [--error-unmatch] [--recurse-submodules] [--others] [--exclude-standard] [<path> ...]
git ls-remote [<remote>]
git ls-tree [-r] [-d] [-l/--long] [--name-only] [--name-status] [--abbrev=<n>] <ref> [<file> ...]
git merge (--continue | --abort | --skip)
git merge <ref>
git merge-base [--is-ancestor] <ref> <ref>
git mv <file> <file>
git pull [--rebase] [--ff-only]
git push [-u/--set-upstream] [--force-with-lease] [<remote> [<name>#locki-<wt-id>]]
git rebase (--continue | --abort | --skip)
git rebase <ref>
git reflog [--all]
git remote (-v/--verbose | get-url <remote>)
git reset [--hard] <ref>
git restore [--staged] [--source=<ref>] <file> ...
git rev-list [--count] [--ancestry-path] [<ref>] [<ref>]
git rev-parse [-q/--quiet] [--show-cdup] [--show-toplevel] [--git-dir] [--git-common-dir] [--is-inside-work-tree] [--abbrev-ref] [--symbolic-full-name] [--short] [--verify] [<arg> ...]
git rm [-q/--quiet] [-f/--force] <file> ...
git shortlog [-s/--summary] [-n/--numbered] [<ref>] [<ref>]
git show [<ref>] [--stat] [--oneline] [--name-only] [--name-status] [-s/--no-patch] [-q/--quiet] [--diff-filter=<filter>] [--format=<fmt>] [--pretty=<fmt>] [<file> ...]
git stash [push] [-m/--message=<text>#locki-<wt-id>] [<file> ...]
git stash (pop | drop) [<owned-stash-ref>]
git stash apply [<stash-ref>]
git stash list
git stash show [<stash-ref>]
git status [-s/--short] [-b/--branch] [-u/--untracked-files=<mode>] [--porcelain] [--ignore-submodules] [--ahead-behind] [<file> ...]
git symbolic-ref [-q/--quiet] <ref>
git switch ([--create | --force-create] <name>#locki-<wt-id> [<start-point>] | --detach <ref>)
git tag [-l/--list] [--sort=<key>] [<pattern>]
```

`<wt-id>` is the 8-char slug in worktree directory name: `.../locki/worktrees/<repo-name>-locki-<wt-id>`. Branches you create, modify, or switch to must be named matching this pattern: `<name>#locki-<wt-id>`. You may read from any ref. `<owned-stash-ref>` is a stash whose message contains `#locki-<wt-id>` -- only those can be popped or dropped; any stash can be applied.

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
gh -v/--version
gh api (repos/<owner>/<repo>/pulls/<number>/comments | repos/<owner>/<repo>/pulls/<number>/reviews | repos/<owner>/<repo>/dependabot/alerts | repos/<owner>/<repo>/dependabot/alerts/<number> | repos/<owner>/<repo>/code-scanning/alerts | repos/<owner>/<repo>/code-scanning/alerts/<number> | repos/<owner>/<repo>/secret-scanning/alerts | repos/<owner>/<repo>/secret-scanning/alerts/<number>) [-X/--method=GET] [-q/--jq=<expr>]
gh auth status
gh issue (view [<number>] [--comments] [--json=<fields>] [-q/--jq=<expr>] | list [-L/--limit=<n>] [-s/--state=<state>] [-S/--search=<query>] [-l/--label=<l>] [--author=<a>] [--json=<fields>] [-q/--jq=<expr>])
gh issue comment <number> -b/--body=<b>
gh label list [-L/--limit=<n>]
gh pr (view [<number>] [--comments] [--json=<fields>] [-q/--jq=<expr>] | list [-L/--limit=<n>] [-s/--state=<state>] [-S/--search=<query>] | diff [<number>] [--name-only] [--patch] [--stat] | status | checks [<number>])
gh pr comment <number> -b/--body=<b>
gh pr create -t/--title=<t> [-b/--body=<b>] [-B/--base=<b>] [-H/--head=<h>] [-d/--draft] [-f/--fill] [-r/--reviewer=<r>] [-l/--label=<l>] [-a/--assignee=<a>]
gh pr edit [<number>] [-t/--title=<t>] [-b/--body=<b>] [--add-label=<l>] [--add-reviewer=<r>] [--add-assignee=<a>]
gh pr review <number> (--approve | --comment | --request-changes) [-b/--body=<b>]
gh release (list [-L/--limit=<n>] | view [<tag>])
gh repo view [--json=<fields>] [-q/--jq=<expr>]
gh run (view [<number>] [-j/--job=<number>] [--log] [--log-failed] | list [-L/--limit=<n>] [-w/--workflow=<name>] [--json=<fields>] [-q/--jq=<expr>])
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

Preinstalled: `bun`, `docker`, `fd`, `jq`, `k9s`, `kubectl`, `mise`, `node`/`npm`/`npx`, `pnpm`/`pnpx`, `poetry`, `rg`, `uv`/`uvx`, `yarn`, `yq`.

## Docker builds

`docker build` runs on a BuildKit daemon shared between sandboxes, so layers are cached across sandboxes and repeat builds are fast. One behavioral difference: `FROM` / `COPY --from` resolve images from registries, not from images previously built or loaded into this sandbox's Docker daemon. If a build must consume a locally-built image (e.g. `docker build -t base . && docker build .` where the second Dockerfile has `FROM base`), pass `--builder default` to build on the sandbox-local builder instead (correct, but slower and without the shared cache).


# Startup checklist

Perform always when starting a conversation.

1. Check project metadata (`mise.toml`, `.tool-versions`, `.nvmrc`, `pyproject.toml`, etc.), CI definitions (`.github/workflows/*.yaml`, etc.) or docs (`README.md`, `CONTRIBUTING.md`, `*.md`, `docs/*`, etc.) to determine needed tools and their versions, and setup commands. If there is `mise.toml`, run `mise install` to set up all tools. Otherwise manually enable specific tool versions using e.g.: `mise use -g python@3.12.1`, `mise use -g node@22`, `mise use -g jq`, falling back to OS package manager if `mise` does not have the tool (`dnf` by default, unless running on a custom image).

2. Check current branch name using `git branch --show-current`. If it is `untitled#locki-<wt-id>`, rename using `git branch <new-name>#locki-<wt-id> --move`. Pick `<new-name>` based on the task at hand.

IMPORTANT: The user identifies the sandboxes by the branch name. Do not start anything while the branch is `untitled#locki-<wt-id>` -- always rename first!

3. Perform project-specific install steps (`uv sync`, `npm install`, etc.)
  - Python: If project uses `uv` or `poetry`, use that. If not clear (e.g. just raw `requirements.txt`), use `uv venv --python=<version>` + `uv pip ...`. If you need to use real `pip`, use `uv venv --python=<version> --seed` + `.venv/bin/pip ...`.
