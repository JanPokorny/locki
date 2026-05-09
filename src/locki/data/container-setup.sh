#!/bin/sh
set -eux

# MARK: AI CLIs

# AGENTS.md is injected as base64 (silenced to keep xtrace output readable).
mkdir -p /etc/claude-code /etc/gemini-cli /etc/codex /etc/opencode /etc/copilot/.github/instructions/
set +x
echo '__AGENTS_MD_B64__' | base64 -d | tee /etc/claude-code/CLAUDE.md /etc/gemini-cli/GEMINI.md /etc/codex/AGENTS.md /etc/opencode/AGENTS.md /etc/copilot/.github/instructions/system.instructions.md > /dev/null
set -x

cat > /etc/gemini-cli/settings.json << 'EOF'
{"security": {"folderTrust": {"enabled": false}}, "tools": {"sandbox": false}}
EOF

cat > /etc/codex/config.toml << EOF
approval_policy = "never"
sandbox_mode = "danger-full-access"
cli_auth_credentials_store = "file"
developer_instructions = "/etc/codex/AGENTS.md"
projects."$LOCKI_WORKTREES_HOME".trust_level = "trusted"
EOF

# MARK: High-priority shims

mkdir -p /opt/locki/bin/high

## Command bridge (git, gh, locki → SSH proxy to host)
tee /opt/locki/bin/high/git /opt/locki/bin/high/gh /opt/locki/bin/high/locki > /dev/null << 'EOF'
#!/bin/sh
cmd=$(basename "$0")
set -- "$(pwd)" "$cmd" "$@"
q=""
for arg in "$@"; do
  q="${q:+$q }'$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")'"
done
exec ssh -F /root/.ssh/locki-ssh-config locki-proxy -- "$q"
EOF

## agent-browser: install chromium if missing, set env, then exec real binary
cat > /opt/locki/bin/high/agent-browser << 'EOF'
#!/bin/bash
set -eo pipefail
export PATH="${PATH#*/opt/locki/bin/high:}"
if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  (
    echo "Installing chromium..."
    if command -v dnf >/dev/null 2>&1; then dnf install -yq chromium
    elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -yqq chromium-browser
    fi
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
export AGENT_BROWSER_EXECUTABLE_PATH=$(command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null)
exec agent-browser "$@"
EOF

## node/npm/npx/yarn: install libatomic if missing
for bin in node npm npx yarn; do
  cat > "/opt/locki/bin/high/$bin" << EOF
#!/bin/bash
set -eo pipefail
export PATH="\${PATH#*/opt/locki/bin/high:}"
if ! ldconfig -p 2>/dev/null | grep -q libatomic; then
  (
    echo "Installing libatomic..."
    if command -v dnf >/dev/null 2>&1; then dnf install -yq libatomic
    elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -yqq libatomic1
    fi
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
exec $bin "\$@"
EOF
done

## pnpm: libatomic + configure cache dirs on first use
cat > /opt/locki/bin/high/pnpm << 'EOF'
#!/bin/bash
set -eo pipefail
export PATH="${PATH#*/opt/locki/bin/high:}"
if ! ldconfig -p 2>/dev/null | grep -q libatomic; then
  (
    echo "Installing libatomic..."
    if command -v dnf >/dev/null 2>&1; then dnf install -yq libatomic
    elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -yqq libatomic1
    fi
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
if ! pnpm config get store-dir 2>/dev/null | grep -q /var/cache/locki/pnpm; then
  (
    echo "Configuring pnpm..."
    pnpm config set store-dir /var/cache/locki/pnpm
    pnpm config set global-bin-dir /usr/local/bin
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
exec pnpm "$@"
EOF

## AI harnesses: inject default flags
for pair in \
  "claude=--dangerously-skip-permissions" \
  "gemini=--yolo" \
  "codex=--yolo" \
  "copilot=--yolo --no-auto-update" \
; do
  bin="${pair%%=*}"
  args="${pair#*=}"
  cat > "/opt/locki/bin/high/$bin" << EOF
#!/bin/bash
set -eo pipefail
export PATH="\${PATH#*/opt/locki/bin/high:}"
exec "\$(command -v $bin)" $args "\$@"
EOF
done

chmod +x /opt/locki/bin/high/*

# MARK: Low-priority shims

mkdir -p /opt/locki/bin/low

## Node.js and npm
for bin in node npm npx; do
  cat > "/opt/locki/bin/low/$bin" << EOF
#!/bin/bash
set -eo pipefail
export MISE_ENABLE_TOOLS=""
if ! PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -v '^/opt/locki/bin/' | paste -sd:) node -v >/dev/null 2>&1; then
  (
    echo "Installing Node.js..."
    if command -v dnf >/dev/null 2>&1; then dnf install -yq nodejs npm
    elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -yqq nodejs npm
    fi
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
export PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -v '^/opt/locki/bin/low\$' | paste -sd:)
exec $bin "\$@"
EOF
done

## NPM packages
for pair in \
  "@anthropic-ai/claude-code=claude" \
  "@google/gemini-cli=gemini" \
  "@mariozechner/pi-coding-agent=pi" \
  "@openai/codex=codex" \
  "agent-browser=agent-browser" \
; do
  pkg="${pair%%=*}"
  bin="${pair#*=}"
  cat > "/opt/locki/bin/low/$bin" << EOF
#!/bin/bash
set -eo pipefail
if ! PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -v '^/opt/locki/bin/' | paste -sd:) command -v $bin >/dev/null 2>&1; then
  (
    echo "Installing $pkg..."
    npm install -g $pkg
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
export PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -v '^/opt/locki/bin/low\$' | paste -sd:)
exec $bin "\$@"
EOF
done

## Mise packages
for pair in \
  "fd=fd" \
  "github:anomalyco/opencode=opencode" \
  "github:github/copilot-cli=copilot" \
  "jq=jq" \
  "k9s=k9s" \
  "kubectl=kubectl" \
  "pnpm=pnpm" \
  "rg=rg" \
  "uv=uv" \
  "yarn=yarn" \
  "yq=yq" \
; do
  pkg="${pair%%=*}"
  bin="${pair#*=}"
  cat > "/opt/locki/bin/low/$bin" << EOF
#!/bin/bash
set -eo pipefail
if ! PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -v '^/opt/locki/bin/' | paste -sd:) command -v $bin >/dev/null 2>&1; then
  (
    echo "Installing $pkg..."
    MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true mise use -g $pkg
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
export PATH=\$(printf '%s' "\$PATH" | tr ':' '\\n' | grep -v '^/opt/locki/bin/low\$' | paste -sd:)
exec $bin "\$@"
EOF
done

## bwrap: no-op shim so Codex doesn't complain at startup
cat > /opt/locki/bin/low/bwrap << 'EOF'
#!/bin/sh
exec bwrap "$@"
EOF

## Docker
cat > /opt/locki/bin/low/docker << 'EOF'
#!/bin/bash
set -eo pipefail
if ! PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/opt/locki/bin/' | paste -sd:) command -v docker >/dev/null 2>&1; then
  (
    echo "Installing Docker..."
    if command -v dnf >/dev/null 2>&1; then
      dnf install -yq moby-engine docker-compose docker-buildx docker-buildkit
    else
      echo "Error: unsupported distro, install Docker manually (https://get.docker.com/)"
      exit 1
    fi
    systemctl enable --now docker
  ) 2>&1 | sed 's/^/[locki auto install] /' >&2
fi
export PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/opt/locki/bin/low$' | paste -sd:)
exec docker "$@"
EOF

chmod +x /opt/locki/bin/low/*

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
timeout 30s sh -c 'while ! ping -c1 -W1 connectivitycheck.gstatic.com >/dev/null 2>&1; do sleep 1; done'

# MARK: Mise

if ! command -v mise; then
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
      echo "[Locki] Error: no HTTP client found (need curl, wget, or python3)" >&2
      exit 1
    fi
    if [ "$(sha256sum "$tmpdir/$mise_file" | cut -d' ' -f1)" != "$checksum" ]; then echo "checksum mismatch" >&2; exit 1; fi

    mkdir -p "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"
    cd "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"
    if [ "$ext" = "tar.zst" ]; then zstd -d -c "$tmpdir/$mise_file" | tar -xf -; else tar -xf "$tmpdir/$mise_file"; fi
  fi

  chmod +x "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}/mise/bin/mise"
  ln -sf "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}/mise/bin/mise" /usr/local/bin/mise
  chmod +x /usr/local/bin/mise
fi
