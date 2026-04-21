#!/bin/sh
set -eux

# MARK: AI CLIs

# AGENTS.md is injected as base64 (silenced to keep xtrace output readable).
mkdir -p /etc/claude-code /etc/gemini-cli /etc/codex /etc/opencode
set +x
echo '__AGENTS_MD_B64__' | base64 -d | tee /etc/claude-code/CLAUDE.md /etc/gemini-cli/GEMINI.md /etc/codex/AGENTS.md /etc/opencode/AGENTS.md > /dev/null
set -x

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

# MARK: High-priority shims

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

## pnpm -- set store dir
cat > /opt/locki/bin/pnpm << '__LOCKI_EOF__'
#!/bin/sh
rm -f "$(readlink -f "$0")"
pnpm config set store-dir /var/cache/locki/pnpm
pnpm config set global-bin-dir /usr/local/bin
exec pnpm "$@"
__LOCKI_EOF__

chmod +x /opt/locki/bin/*

# MARK: Low-priority shims

mkdir -p /opt/locki/bin/jit

## bwrap executable needs to be present so Codex shuts up about it
cat > /opt/locki/bin/jit/bwrap << '__LOCKI_EOF__'
#!/bin/sh
rm -f "$(readlink -f "$0")"
exec bwrap "$@"
__LOCKI_EOF__

## JIT shim for docker -- not installable with Mise
command -v docker || cat > /opt/locki/bin/jit/docker << '__LOCKI_EOF__'
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

## JIT shim for rodney -- go-rod can't self-download Chrome on aarch64 Linux.
## (preinstall on Fedora, other OS may need manual Chromium install, agent will figure it out)
cat > /opt/locki/bin/jit/rodney << '__LOCKI_EOF__'
#!/bin/sh
export MISE_STATUS_MESSAGE_MISSING_TOOLS=never
if ! test -x /usr/lib64/chromium-browser/headless_shell && command -v dnf >/dev/null 2>&1; then
  dnf install -y chromium-headless
fi
if test -x /usr/lib64/chromium-browser/headless_shell; then
  export ROD_CHROME_BIN=/usr/lib64/chromium-browser/headless_shell
fi
target="$(pwd)"
cd /
exec mise x -C "$target" github:simonw/rodney -- rodney "$@"
__LOCKI_EOF__

## JIT shims for nodejs-based tools
for pair in \
  "npm:@anthropic-ai/claude-code=claude --dangerously-skip-permissions" \
  "npm:@google/gemini-cli=gemini --yolo" \
  "npm:@mariozechner/pi-coding-agent=pi" \
  "npm:@openai/codex=codex --yolo" \
; do
  pkg="${pair%%=*}"
  cmd="${pair##*=}"
  bin="${cmd%% *}"
  cat > "/opt/locki/bin/jit/$bin" << EOF
#!/bin/sh
export MISE_STATUS_MESSAGE_MISSING_TOOLS=never
# run from root to avoid mise discovering mise.toml and triggering full install
target="\$(pwd)"
cd /
# set global node version to avoid broken mise shim
if test "\$(mise tool node --requested)" = "[none]"; then mise use -g node@24; fi
exec mise x -C "\$target" nodejs@24 -- mise x $pkg@\$version -- $cmd "\$@"
EOF
done

## JIT shims for other tools
for pair in \
  "aqua:fish-shell/fish-shell=fish" \
  "fd=fd" \
  "github:anomalyco/opencode=opencode" \
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
  cmd="${pair##*=}"
  bin="${cmd%% *}"
  cat > "/opt/locki/bin/jit/$bin" << EOF
#!/bin/sh
export MISE_STATUS_MESSAGE_MISSING_TOOLS=never
# run from root to avoid mise discovering mise.toml and triggering full install
target="\$(pwd)"
cd /  
exec mise x -C "\$target" $pkg -- $cmd "\$@"
EOF
done

chmod +x /opt/locki/bin/jit/*

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
      echo "Error: no HTTP client found (need curl, wget, or python3)" >&2
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
