#!/bin/sh
set -eux

mkdir -p /etc/claude-code /etc/gemini-cli /etc/codex /etc/opencode \
         /opt/locki/bin /etc/bashrc.d

tee /etc/claude-code/CLAUDE.md /etc/gemini-cli/GEMINI.md \
    /etc/codex/AGENTS.md /etc/opencode/AGENTS.md > /dev/null << '__LOCKI_EOF__'
You are running inside a locki sandbox VM. This is an ephemeral environment designed to keep the main machine safe from malfunctioning agents. The folder is a fresh worktree: before delving into your task, start by setting up the environment. Check project metadata (`mise.toml`, `.tool-versions`, `.nvmrc`, `pyproject.toml`, etc.), CI definitions (`.github/workflows/*.yaml`, etc.) or docs (`README.md`, `CONTRIBUTING.md`, `*.md`, `docs/*`, etc.) to determine needed tools and their versions, and setup commands. If there is `mise.toml`, run `mise install` to set up all tools. Otherwise manually enable specific tool versions using e.g.: `mise use -g python@3.12.1`, `mise use -g node@22`, `mise use -g jq`, falling back to OS package manager if `mise` does not have the tool (`dnf` with RPM Fusion by default, unless running on a custom image).

`git` and `gh` are available but restricted to a safe subset of commands (enforced by the host). Only long flags (`--flag` or `--flag=value`) are accepted; short flags (`-x`) are rejected. Allowed commands:
  git status | diff [--staged] [--name-only] [--stat] [--name-status] [ref [ref]] | add [--all] [file ...] | commit --message=<msg> | push | fetch | log [--oneline] [--format=<fmt>] [--max-count=<n>] [ref] | show [ref] | restore [--staged] [--source=ref] <file ...>
  git switch <branch> | switch --create=<branch>  (only the initial branch and sub-branches under it)
  git stash push [--message=<msg>] | stash list | stash pop [ref] | stash apply [ref] | stash drop [ref]  (stashes are branch-scoped)
  gh pr create [--title=<t>] [--body=<b>] [--base=<b>] [--head=<h>] [--draft] [--fill] [--reviewer=<r>] [--label=<l>] [--assignee=<a>] | gh pr view/list/diff/status | gh run view/list | gh issue view/list
Any other git/gh invocation will be rejected by the proxy.
__LOCKI_EOF__

cat > /etc/gemini-cli/settings.json << '__LOCKI_EOF__'
{"security": {"folderTrust": {"enabled": false}}, "tools": {"sandbox": false}}
__LOCKI_EOF__

cat > /etc/codex/config.toml << __LOCKI_EOF__
approval_policy = "never"
sandbox_mode = "danger-full-access"
cli_auth_credentials_store = "file"
developer_instructions = "/etc/codex/AGENTS.md"
projects."$LOCKI_WORKTREES_HOME".trust_level = "trusted"
__LOCKI_EOF__

cat > /opt/locki/bin/git << '__LOCKI_EOF__'
#!/bin/bash
cmd=$(basename "$0")
set -- "$(pwd)" "$cmd" "$@"
q=""
for arg in "$@"
    do q="${q:+$q }'${arg//\'/\'\\\'\'}'";
done
exec ssh -F /root/.ssh/locki-ssh-config locki-proxy -- "$q"
__LOCKI_EOF__
cp /opt/locki/bin/git /opt/locki/bin/gh

cat > /opt/locki/bin/bwrap << '__LOCKI_EOF__'
#!/bin/sh
exit 1
__LOCKI_EOF__

cat > /opt/locki/bin/dnf << '__LOCKI_EOF__'
#!/bin/bash
set -euo pipefail
mkdir -p /etc/dnf
echo -e "cachedir=/var/cache/locki/dnf\nkeepcache=1" >> /etc/dnf/dnf.conf
/bin/dnf install -y \
  https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm \
  https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm \
  2>/dev/null || true
rm -f /opt/locki/bin/dnf
exec /bin/dnf "$@"
__LOCKI_EOF__

cat > /opt/locki/bin/apt << '__LOCKI_EOF__'
#!/bin/bash
set -euo pipefail
mkdir -p /etc/apt/apt.conf.d
printf 'Dir::Cache "/var/cache/locki/apt/cache";\nDir::State "/var/cache/locki/apt/state";\n' > /etc/apt/apt.conf.d/99local-cache
rm -f /opt/locki/bin/apt
exec /bin/apt "$@"
__LOCKI_EOF__

cat > /etc/bashrc.d/locki-mise.sh << '__LOCKI_EOF__'
eval "$(mise activate bash)"
__LOCKI_EOF__

cat > /opt/locki/bin/claude << '__LOCKI_EOF__'
#!/bin/bash
mise install nodejs@24 >&2
mise exec nodejs@24 -- mise install npm:@anthropic-ai/claude-code@latest >&2
exec mise exec nodejs@24 npm:@anthropic-ai/claude-code@latest -- claude --dangerously-skip-permissions "$@"
__LOCKI_EOF__

cat > /opt/locki/bin/gemini << '__LOCKI_EOF__'
#!/bin/bash
mise install nodejs@24 >&2
mise exec nodejs@24 -- mise install npm:@google/gemini-cli@latest >&2
exec mise exec nodejs@24 npm:@google/gemini-cli@latest -- gemini --yolo "$@"
__LOCKI_EOF__

cat > /opt/locki/bin/codex << '__LOCKI_EOF__'
#!/bin/bash
mise install nodejs@24 >&2
mise exec nodejs@24 -- mise install npm:@openai/codex@latest >&2
exec mise exec nodejs@24 npm:@openai/codex@latest -- codex --yolo "$@"
__LOCKI_EOF__

cat > /opt/locki/bin/opencode << '__LOCKI_EOF__'
#!/bin/bash
mise use -g github:anomalyco/opencode >&2
exec opencode "$@"
__LOCKI_EOF__

chmod +x /opt/locki/bin/*
hostnamectl set-hostname locki 2>/dev/null || echo locki > /etc/hostname

if ! command -v mise >/dev/null 2>&1; then
  if [ -x /bin/dnf ]; then /bin/dnf -y copr enable jdxcode/mise && /bin/dnf -y install mise
  elif [ -x /bin/apt ]; then /bin/apt update -y && /bin/apt install -y mise
  elif [ -x /bin/pacman ]; then /bin/pacman -Sy --noconfirm mise
  elif [ -x /bin/apk ]; then /bin/apk add mise
  elif [ -x /bin/zypper ]; then /bin/zypper --non-interactive install mise
  else curl -fsSL https://mise.run | sh
  fi
fi

echo '192.168.5.2 host.lima.internal' >> /etc/hosts
