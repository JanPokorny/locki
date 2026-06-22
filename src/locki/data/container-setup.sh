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

# MARK: libatomic
## Node 25+ needs it, distros don't ship it, Locki vendors it

if ! ldconfig -p 2>/dev/null | grep -q libatomic; then
  set +x
  libatomic_b64='__LIBATOMIC_B64__'
  set -x
  if [ -n "$libatomic_b64" ]; then
    mkdir -p /usr/local/lib
    echo "$libatomic_b64" | base64 -d > /usr/local/lib/libatomic.so.1
    mkdir -p /etc/ld.so.conf.d
    echo /usr/local/lib > /etc/ld.so.conf.d/locki.conf
    ldconfig 2>/dev/null || true
  fi
fi

# MARK: High-priority shims

mkdir -p /opt/locki/bin/high

## helper script for auto-install
cat > /opt/locki/bin/high/locki-auto-install << 'EOF'
#!/bin/sh
name="$1"
shift
log="/var/log/locki/install/${name}.log"
mkdir -p "$(dirname "$log")" /var/cache/locki
printf '\033[1;35mᚠ\033[0m Installing %s...\n' "$name" >&2
# Always log; mirror to the terminal too when stderr is a TTY (user-run), stay silent for agents (no TTY).
[ -t 2 ] && tty_out=/dev/stderr || tty_out=/dev/null
# flock is not reentrant: an install command may invoke another shim that auto-installs (e.g. mise),
# which would deadlock on the lock its own ancestor holds. Take the lock only at the outermost call.
[ -n "${LOCKI_INSTALLING:-}" ] || set -- flock -o /var/cache/locki/.install.lock env LOCKI_INSTALLING=1 "$@"
{ "$@" 2>&1; echo "$?" > "$log.rc"; } | tee -a "$log" > "$tty_out"
rc=$(cat "$log.rc"); rm -f "$log.rc"
if [ "$rc" = 0 ]; then
  printf '\033[1;32mᛝ\033[0m Installed %s\n' "$name" >&2
else
  printf '\033[1;31mᛞ\033[0m Failed to install %s, see log at \033[33m%s\033[0m\n' "$name" "$log" >&2
  [ -t 2 ] || tail -n 30 "$log" >&2
  exit 1
fi
EOF

## locki-command-real: resolve binary outside ALL /opt/locki/bin/ shim folders
cat > /opt/locki/bin/high/locki-command-real << 'EOF'
#!/bin/sh
_mise="${MISE_INSTALL_PATH:-/usr/local/bin/mise}"
PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v -e '^/opt/locki/bin/' -e '/mise/shims' | paste -sd:) command -v "$1" || "$_mise" which "$1" 2>/dev/null || exit 1
EOF

## locki-command-real-or-autoinstalled: resolve binary outside /opt/locki/bin/high (low shims still reachable)
cat > /opt/locki/bin/high/locki-command-real-or-autoinstalled << 'EOF'
#!/bin/sh
_mise="${MISE_INSTALL_PATH:-/usr/local/bin/mise}"
"$_mise" which "$1" 2>/dev/null || PATH=$(printf '%s' "${PATH#*/opt/locki/bin/high:}" | tr ':' '\n' | grep -v '/mise/shims' | paste -sd:) command -v "$1" || exit 1
EOF

## Command bridge (git, gh, locki → SSH proxy to host)
## When cwd is outside the worktree tree, run the real binary directly in sandbox.
tee /opt/locki/bin/high/git /opt/locki/bin/high/gh /opt/locki/bin/high/locki > /dev/null << 'EOF'
#!/bin/sh
cmd=$(basename "$0")
cwd=$(pwd)
case "$cwd" in "$LOCKI_WORKTREES_HOME"/*)
  bridge=1
  if [ "$cmd" = git ]; then
    skip=0
    for arg in "$@"; do
      if [ "$skip" = 1 ]; then skip=0; continue; fi
      case "$arg" in
        -c|-C|--git-dir|--work-tree|--namespace) skip=1 ;;
        clone) bridge=0; break ;;
        -*) : ;;
        *) break ;;
      esac
    done
  fi
  if [ "$bridge" = 1 ]; then
    set -- "$cwd" "$cmd" "$@"
    q=""
    for arg in "$@"; do
      q="${q:+$q }'$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")'"
    done
    exec ssh -F /root/.ssh/locki-ssh-config locki-proxy -- "$q"
  fi
esac
if [ "$cmd" = git ] && ! locki-command-real git >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install git sh -c 'if command -v dnf >/dev/null 2>&1; then dnf install -yq git; elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -yqq git; elif command -v apk >/dev/null 2>&1; then apk add --no-cache git; fi'
fi
exec "$(locki-command-real-or-autoinstalled "$cmd")" "$@"
EOF

## agent-browser: install chromium if missing, set env, then exec real binary
cat > /opt/locki/bin/high/agent-browser << 'EOF'
#!/bin/bash
set -eo pipefail
if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install chromium sh -c 'if command -v dnf >/dev/null 2>&1; then dnf install -yq chromium; elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -yqq chromium-browser; fi'
fi
export AGENT_BROWSER_EXECUTABLE_PATH=$(command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null)
exec "$(locki-command-real-or-autoinstalled agent-browser)" "$@"
EOF

## node/npm/npx: install Node.js if missing via mise
for bin in node npm npx; do
  cat > "/opt/locki/bin/high/$bin" << EOF
#!/bin/bash
set -eo pipefail
if ! locki-command-real node >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install nodejs sh -c 'export MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true; mise use -g node && mise install node' >/dev/null 2>&1
fi
exec "\$(locki-command-real-or-autoinstalled $bin)" "\$@"
EOF
done

## pnpm: cache + global virtual store
cat > /opt/locki/bin/high/pnpm << 'EOF'
#!/bin/bash
set -eo pipefail
_real=$(locki-command-real-or-autoinstalled pnpm) || exit 1
if ! "$_real" config get enable-global-virtual-store 2>/dev/null | grep -q true; then
  /opt/locki/bin/high/locki-auto-install pnpm sh -c "\"$_real\" config set store-dir /var/cache/locki/pnpm && \"$_real\" config set global-bin-dir /usr/local/bin && \"$_real\" config set enable-global-virtual-store true && \"$_real\" config delete virtual-store-dir 2>/dev/null || true"
fi
exec "$_real" "$@"
EOF

## uv: symlink .venv to btrfs
cat > /opt/locki/bin/high/uv << 'EOF'
#!/bin/bash
set -eo pipefail
_real=$(locki-command-real-or-autoinstalled uv) || exit 1
if _dir="$("$_real" workspace dir 2>/dev/null)"; then
  export UV_PROJECT_ENVIRONMENT="/var/cache/locki/uv-venvs${_dir}/.venv"
  ln -sfn "$UV_PROJECT_ENVIRONMENT" "$_dir/.venv" 2>/dev/null || true
fi
exec "$_real" "$@"
EOF

## yarn: symlink node_modules to btrfs
cat > /opt/locki/bin/high/yarn << 'EOF'
#!/bin/bash
set -eo pipefail
if _dir="$("$(locki-command-real-or-autoinstalled npm)" prefix 2>/dev/null)"; then
  mkdir -p "/var/cache/locki/node-modules${_dir}/node_modules"
  ln -sfn "/var/cache/locki/node-modules${_dir}/node_modules" "$_dir/node_modules" 2>/dev/null || true
fi
exec "$(locki-command-real-or-autoinstalled yarn)" "$@"
EOF

## bun: symlink node_modules to btrfs + redirect cache
cat > /opt/locki/bin/high/bun << 'EOF'
#!/bin/bash
set -eo pipefail
if _dir="$("$(locki-command-real-or-autoinstalled npm)" prefix 2>/dev/null)"; then
  mkdir -p "/var/cache/locki/node-modules${_dir}/node_modules"
  ln -sfn "/var/cache/locki/node-modules${_dir}/node_modules" "$_dir/node_modules" 2>/dev/null || true
fi
exec "$(locki-command-real-or-autoinstalled bun)" "$@"
EOF

chmod +x /opt/locki/bin/high/*

# MARK: Low-priority shims

mkdir -p /opt/locki/bin/low

## Claude Code: official RPM repo on dnf distros, npm fallback elsewhere
cat > /opt/locki/bin/low/claude << 'EOF'
#!/bin/bash
set -eo pipefail
if ! locki-command-real claude >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    /opt/locki/bin/high/locki-auto-install claude-code sh -c '
      printf "[claude-code]\nname=Claude Code\nbaseurl=https://downloads.claude.ai/claude-code/rpm/latest\nenabled=1\ngpgcheck=1\ngpgkey=https://downloads.claude.ai/keys/claude-code.asc\n" > /etc/yum.repos.d/claude-code.repo
      dnf install -yq claude-code
    '
  else
    locki-command-real node >/dev/null 2>&1 || /opt/locki/bin/high/locki-auto-install nodejs sh -c 'export MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true; mise use -g node && mise install node' >/dev/null 2>&1
    /opt/locki/bin/high/locki-auto-install @anthropic-ai/claude-code sh -c 'export MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true; mise use -g npm:@anthropic-ai/claude-code && mise install npm:@anthropic-ai/claude-code'
  fi
fi
exec "$(locki-command-real claude)" "$@"
EOF

## NPM packages
for pair in \
  "@google/gemini-cli=gemini" \
  "@mariozechner/pi-coding-agent=pi" \
  "@openai/codex=codex" \
  "agent-browser=agent-browser" \
  "corepack=corepack" \
; do
  pkg="${pair%%=*}"
  bin="${pair#*=}"
  cat > "/opt/locki/bin/low/$bin" << EOF
#!/bin/bash
set -eo pipefail
if ! locki-command-real $bin >/dev/null 2>&1; then
  locki-command-real node >/dev/null 2>&1 || /opt/locki/bin/high/locki-auto-install nodejs sh -c 'export MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true; mise use -g node && mise install node' >/dev/null 2>&1
  /opt/locki/bin/high/locki-auto-install $pkg sh -c 'export MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true; mise use -g npm:$pkg && mise install npm:$pkg'
fi
exec "\$(locki-command-real $bin)" "\$@"
EOF
done

## Corepack packages
for pair in \
  "pnpm=pnpm" \
  "pnpm=pnpx" \
  "pnpm=pnx" \
  "yarn=yarn" \
; do
  pkg="${pair%%=*}"
  bin="${pair#*=}"
  cat > "/opt/locki/bin/low/$bin" << EOF
#!/bin/bash
set -eo pipefail
if ! locki-command-real $bin >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install $pkg corepack enable $pkg
fi
exec "\$(locki-command-real $bin)" "\$@"
EOF
done

## Mise packages
for pair in \
  "bun=bun" \
  "fd=fd" \
  "github:anomalyco/opencode=opencode" \
  "github:github/copilot-cli=copilot" \
  "jq=jq" \
  "k9s=k9s" \
  "kubectl=kubectl" \
  "poetry=poetry" \
  "rg=rg" \
  "uv=uv" \
  "uv=uvx" \
  "yq=yq" \
; do
  pkg="${pair%%=*}"
  bin="${pair#*=}"
  cat > "/opt/locki/bin/low/$bin" << EOF
#!/bin/bash
set -eo pipefail
if ! locki-command-real $bin >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install $pkg sh -c 'export MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true; mise use -g $pkg && mise install $pkg'
fi
exec "\$(locki-command-real $bin)" "\$@"
EOF
done

## bwrap: no-op shim so Codex doesn't complain at startup
cat > /opt/locki/bin/low/bwrap << 'EOF'
#!/bin/sh
exec "$(locki-command-real bwrap)" "$@"
EOF

## Mise
cat > /opt/locki/bin/low/mise << 'EOF'
#!/bin/bash
set -eo pipefail
if ! locki-command-real mise >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install mise sh -c '
    set -eu
    mise_version="2026.4.10"
    musl=""; if ldd /bin/ls 2>/dev/null | grep musl; then musl="-musl"; fi
    case "$(uname -m)" in x86_64) arch="x64$musl";; aarch64|arm64) arch="arm64$musl";; esac
    if ! test -d "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"; then
      ext="tar.gz"
      if command -v zstd >/dev/null 2>&1 && tar --version 2>/dev/null | grep -q "1\.\(3[1-9]\|[4-9][0-9]\)"; then ext="tar.zst"; fi
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
      if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$tmpdir/$mise_file" "$mise_url"
      elif command -v wget >/dev/null 2>&1; then wget -qO "$tmpdir/$mise_file" "$mise_url"
      elif command -v python3 >/dev/null 2>&1; then python3 -c "from urllib.request import urlretrieve,install_opener,build_opener;o=build_opener();o.addheaders=[(\"User-Agent\",\"curl/8\")];install_opener(o);urlretrieve(\"$mise_url\",\"$tmpdir/$mise_file\")"
      else echo "[Locki] Error: no HTTP client found (need curl, wget, or python3)" >&2; exit 1; fi
      if [ "$(sha256sum "$tmpdir/$mise_file" | cut -d" " -f1)" != "$checksum" ]; then echo "checksum mismatch" >&2; exit 1; fi
      mkdir -p "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"
      cd "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}"
      if [ "$ext" = "tar.zst" ]; then zstd -d -c "$tmpdir/$mise_file" | tar -xf -; else tar -xf "$tmpdir/$mise_file"; fi
    fi
    chmod +x "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}/mise/bin/mise"
    ln -sf "/var/cache/mise-install/mise-v${mise_version}-linux-${arch}/mise/bin/mise" /usr/local/bin/mise
    chmod +x /usr/local/bin/mise
  '
fi
exec "$(locki-command-real mise)" "$@"
EOF

## Docker
cat > /opt/locki/bin/low/docker << 'EOF'
#!/bin/bash
set -eo pipefail
if ! locki-command-real docker >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install docker sh -c '
    if command -v dnf >/dev/null 2>&1; then
      dnf install -yq moby-engine docker-compose docker-buildx docker-buildkit
    else
      echo "Error: unsupported distro, install Docker manually (https://get.docker.com/)"
      exit 1
    fi
    systemctl enable --now containerd docker
  '
fi
_real=$(locki-command-real docker)

sock=/var/cache/locki/buildkit.sock
is_build=
case "${1:-}" in
  build) is_build=1 ;;
  buildx) [ "${2:-}" = build ] && is_build=1 ;;
  image) [ "${2:-}" = build ] && is_build=1 ;;
esac

timeout 60 sh -c 'until "$0" info >/dev/null 2>&1; do sleep 0.5; done' "$_real" || true

if [ -n "$is_build" ] && [ -S "$sock" ]; then
  for a in "$@"; do case "$a" in --builder|--builder=*) exec "$_real" "$@" ;; esac; done
  [ -f "${HOME:-/root}/.docker/buildx/instances/locki" ] \
    || "$_real" buildx create --name locki --driver remote "unix://$sock" >/dev/null 2>&1 || true
  case "$1" in build) shift ;; *) shift 2 ;; esac
  has_output=
  for a in "$@"; do case "$a" in --load|--push|--output*|-o*) has_output=1 ;; esac; done
  set -- buildx build --builder locki "$@"
  [ -z "$has_output" ] && set -- "$@" --load
fi
exec "$_real" "$@"
EOF

chmod +x /opt/locki/bin/low/*

# MARK: Caching

if command -v apt-get >/dev/null 2>&1; then
  mkdir -p /etc/apt/apt.conf.d /var/cache/locki/apt/cache /var/cache/locki/apt/state
  printf 'Dir::Cache "/var/cache/locki/apt/cache";\nDir::State "/var/cache/locki/apt/state";\n' > /etc/apt/apt.conf.d/99local-cache
fi

if command -v dnf >/dev/null 2>&1; then
  mkdir -p /etc/dnf /var/cache/locki/dnf
  printf "system_cachedir=/var/cache/locki/dnf\nkeepcache=1\ntsflags=nodocs\ninstall_weak_deps=False\nfastestmirror=True\n" >> /etc/dnf/dnf.conf
  mkdir -p /etc/rpm
  printf '%%_install_langs en_US:en\n' >> /etc/rpm/macros.locki
fi

ln -sfn /var/cache/locki $HOME/.cache


# MARK: Networking

hostnamectl set-hostname locki 2>/dev/null || echo locki > /etc/hostname

echo '192.168.5.2 host.lima.internal' >> /etc/hosts

## network is not available for a short while, wait for it
timeout 30s sh -c 'while ! ping -c1 -W1 connectivitycheck.gstatic.com >/dev/null 2>&1; do sleep 1; done'

## transparent container image registry caching
ca_tmp=$(mktemp)
ca_url=http://10.99.0.1/locki-ca.crt
if { command -v curl >/dev/null 2>&1 && curl -fsS --retry 3 -o "$ca_tmp" "$ca_url"; } \
  || { command -v wget >/dev/null 2>&1 && wget -qO "$ca_tmp" "$ca_url"; } \
  || { command -v python3 >/dev/null 2>&1 && python3 -c "from urllib.request import urlretrieve; urlretrieve('$ca_url', '$ca_tmp')"; }; then
  ca_installed=""
  if command -v update-ca-trust >/dev/null 2>&1; then
    mkdir -p /etc/pki/ca-trust/source/anchors
    cp "$ca_tmp" /etc/pki/ca-trust/source/anchors/locki-ca.crt
    update-ca-trust
    ca_installed=1
  elif command -v update-ca-certificates >/dev/null 2>&1; then
    mkdir -p /usr/local/share/ca-certificates
    cp "$ca_tmp" /usr/local/share/ca-certificates/locki-ca.crt
    update-ca-certificates
    ca_installed=1
  fi
  if [ -n "$ca_installed" ]; then
    echo '10.99.0.1 registry-1.docker.io mirror.gcr.io gcr.io ghcr.io quay.io registry.access.redhat.com' >> /etc/hosts
  fi
fi
rm -f "$ca_tmp"
