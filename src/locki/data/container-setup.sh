#!/bin/sh
set -eux

# MARK: AI CLIs

mkdir -p /etc/claude-code /etc/gemini-cli /etc/codex /etc/opencode
tee /etc/claude-code/CLAUDE.md /etc/gemini-cli/GEMINI.md /etc/codex/AGENTS.md /etc/opencode/AGENTS.md > /dev/null << '__LOCKI_EOF__'
You are running inside a locki sandbox VM. This is an ephemeral environment designed to keep the main machine safe from malfunctioning agents. The folder is a fresh worktree: before delving into your task, start by setting up the environment. Check project metadata (`mise.toml`, `.tool-versions`, `.nvmrc`, `pyproject.toml`, etc.), CI definitions (`.github/workflows/*.yaml`, etc.) or docs (`README.md`, `CONTRIBUTING.md`, `*.md`, `docs/*`, etc.) to determine needed tools and their versions, and setup commands. If there is `mise.toml`, run `mise install` to set up all tools. Otherwise manually enable specific tool versions using e.g.: `mise use -g python@3.12.1`, `mise use -g node@22`, `mise use -g jq`, falling back to OS package manager if `mise` does not have the tool (`dnf` by default, unless running on a custom image). Docker is pre-installed.

Some commands execute on the host using a self-service proxy. Run them as usual, non-matches will be rejected. Only long flags are accepted. Available commands:
  git status | diff [--staged] [--name-only] [--stat] [--name-status] [ref [ref]] | add [--all] [file ...] | commit --message=<msg> | push | fetch | log [--oneline] [--format=<fmt>] [--max-count=<n>] [ref] | show [ref] | restore [--staged] [--source=ref] <file ...>
  git switch <branch> | switch --create=<branch>  (where <branch> can be the initial branch or any branch name with the initial branch name + / as a prefix)
  git stash push [--message=<msg>] | stash list | stash pop [ref] | stash apply [ref] | stash drop [ref]  (stashes are branch-scoped)
  gh pr create [--title=<t>] [--body=<b>] [--base=<b>] [--head=<h>] [--draft] [--fill] [--reviewer=<r>] [--label=<l>] [--assignee=<a>] | gh pr view/list/diff/status | gh run view/list | gh issue view/list
  locki port-forward :<container_port> [:<port2> ...]  (When you start a web server, API, or any service the user should access, forward the port to host. The output shows `<host_port>:<container_port>`. Give the user a full URL with the host-side port, e.g. `http://localhost:<host_port>`.)

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

# MARK: Executable shims

mkdir -p /opt/locki/bin

## self-service through ssh proxy
tee /opt/locki/bin/git /opt/locki/bin/gh /opt/locki/bin/locki > /dev/null << '__LOCKI_EOF__'
#!/bin/sh
cmd=$(basename "$0")
set -- "$(pwd)" "$cmd" "$@"
q=""
for arg in "$@"; do
  q="${q:+$q }'$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")'"
done
exec ssh -F /root/.ssh/locki-ssh-config locki-proxy -- "$q"
__LOCKI_EOF__

## bwrap executable needs to be present so Codex shuts up about it
cat > /opt/locki/bin/bwrap << '__LOCKI_EOF__'
#!/bin/sh
rm -f "$(readlink -f "$0")"
exec bwrap "$@"
__LOCKI_EOF__

## auto-install shim for docker -- not installable with Mise
command -v docker || cat > /opt/locki/bin/docker << '__LOCKI_EOF__'
#!/bin/sh
rm -f "$(readlink -f "$0")"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y moby-engine docker-compose docker-buildx docker-buildkit
else
  echo "Error: unsupported distro by the docker auto-install-shim, please install Docker manually (e.g. using the script from https://get.docker.com/)" >&2
  exit 1
fi
systemctl enable --now docker
exec docker "$@"
__LOCKI_EOF__

## nodejs-based auto-install shims
for pair in \
  "npm:@anthropic-ai/claude-code=claude --dangerously-skip-permissions" \
  "npm:@google/gemini-cli=gemini --yolo" \
  "npm:@openai/codex=codex --yolo" \
; do
  pkg="${pair%%=*}"
  cmd="${pair##*=}"
  cat > "/opt/locki/bin/$cmd" << EOF
#!/bin/sh
exec mise x nodejs@24 -- mise x $pkg -- $cmd "\$@"
EOF
done

## other auto-install shims
for pair in \
  "aqua:fish-shell/fish-shell=fish" \
  "kubectl=kubectl" \
  "k9s=k9s" \
  "jq=jq" \
  "yq=yq" \
  "github:anomalyco/opencode=opencode" \
; do
  pkg="${pair%%=*}"
  cmd="${pair##*=}"
  cat > "/opt/locki/bin/$cmd" << EOF
#!/bin/sh
exec mise x $pkg -- $cmd "\$@"
EOF
done

chmod +x /opt/locki/bin/*

# MARK: Caching

mkdir -p /etc/apt/apt.conf.d /var/cache/locki/apt/cache /var/cache/locki/apt/state
printf 'Dir::Cache "/var/cache/locki/apt/cache";\nDir::State "/var/cache/locki/apt/state";\n' > /etc/apt/apt.conf.d/99local-cache

mkdir -p /etc/dnf /var/cache/locki/dnf
printf "cachedir=/var/cache/locki/dnf\nkeepcache=1" >> /etc/dnf/dnf.conf

ln -sfn /var/cache/locki $HOME/.cache

# MARK: Networking

hostnamectl set-hostname locki 2>/dev/null || echo locki > /etc/hostname

echo '192.168.5.2 host.lima.internal' >> /etc/hosts

## network is not available for a short while, wait for it
timeout 30s sh -c 'while ! getent hosts mirrors.fedoraproject.org >/dev/null 2>&1; do sleep 1; done'

# MARK: Mise

if ! test -x /usr/local/bin/mise; then
  mise_version="2026.4.10"

  musl=""; if ldd /bin/ls 2>/dev/null | grep musl; then musl="-musl"; fi
  case "$(uname -m)" in x86_64) arch="x64$musl";; aarch64|arm64) arch="arm64$musl";; esac

  if ! test -d "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"; then
    ext="tar.gz"
    if command -v zstd >/dev/null 2>&1 && tar --version 2>/dev/null | grep -q '1\.\(3[1-9]\|[4-9][0-9]\)'; then ext="tar.zst"; fi

    case "$arch.$ext" in
      x64.tar.gz)         checksum="78e91794c9139ab787c9a4de5e9e63a56d65b16bce60912884cb09f7114f7275";;
      x64-musl.tar.gz)    checksum="6a5fe535fd05e6ac7c525c70a1e05d9b1489ad735a6259c5ff29c7aeb4904b44";;
      arm64.tar.gz)       checksum="03ebfb523239e4f202b19983d0a435e06edae7217694d61b08580ad6afa7a6b4";;
      arm64-musl.tar.gz)  checksum="20876268118bb54471fd3701143f902f48272e59830eeaa2cb06e73012580236";;
      x64.tar.zst)        checksum="d6e9cde12a4b4f38a34d5f9172e1efa4d3522f55b5ce1d42006262b61cf06aa6";;
      x64-musl.tar.zst)   checksum="ac16b3864753836eae7cd9d02ae392abfa4c8a07a65e868217991b820449adb3";;
      arm64.tar.zst)      checksum="3ea02ac3c1354ba69a4d5cebb1416c505206080cfdf58b6019929cc5981e44b8";;
      arm64-musl.tar.zst) checksum="e9180302e01e2586c32f97004022cf924cd39f46b9853679e979efdabc4187a8";;
      *) echo "no checksum for linux-$arch.$ext" >&2; exit 1;;
    esac

    tmpdir=$(mktemp -d)
    trap "rm -rf \"$tmpdir\"" EXIT
    mise_file="mise-v$mise_version-linux-$arch.$ext"
    mise_url="https://mise.jdx.dev/v$mise_version/$mise_file"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL -o "$tmpdir/$mise_file" "$mise_url"
    elif command -v wget >/dev/null 2>&1; then
      wget -qO "$tmpdir/$mise_file" "$mise_url"
    elif command -v python3 >/dev/null 2>&1; then
      python3 -c "from urllib.request import urlretrieve,install_opener,build_opener;o=build_opener();o.addheaders=[('User-Agent','curl/8')];install_opener(o);urlretrieve('$mise_url','$tmpdir/$mise_file')"
    else
      echo "Error: no HTTP client found (need curl, wget, or python3)" >&2
      exit 1
    fi
    if [ "$(sha256sum "$tmpdir/$mise_file" | cut -d' ' -f1)" != "$checksum" ]; then echo "checksum mismatch" >&2; exit 1; fi

    mkdir -p "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"
    cd "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"
    if [ "$ext" = "tar.zst" ]; then zstd -d -c "$tmpdir/$mise_file" | tar -xf -; else tar -xf "$tmpdir/$mise_file"; fi
  fi

  ln -sf "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}/mise/bin/mise" /usr/local/bin/mise
fi

mkdir -p /etc/bashrc.d
/usr/local/bin/mise activate bash >/etc/bashrc.d/mise.sh

mkdir -p /etc/zshrc.d
/usr/local/bin/mise activate zsh >/etc/zshrc.d/mise.sh

mkdir -p /etc/fish/conf.d
/usr/local/bin/mise activate fish >/etc/fish/conf.d/mise.fish