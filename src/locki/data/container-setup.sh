#!/bin/sh
set -eux

# MARK: AI CLIs

# AGENTS.md is injected as base64 (silenced to keep xtrace output readable).
## agy reads no system-wide config: its instructions and settings are seeded into the
## sandbox home instead (see HomeService.prepare).
mkdir -p /etc/claude-code /etc/codex /etc/opencode /etc/copilot/.github/instructions/
set +x
echo '__AGENTS_MD_B64__' | base64 -d | tee /etc/claude-code/CLAUDE.md /etc/codex/AGENTS.md /etc/opencode/AGENTS.md /etc/copilot/.github/instructions/system.instructions.md > /dev/null
set -x

## The codex app-server daemon (>= 0.156) refuses its socket dir unless root or the current
## user owns it; the shared 9p home shows host uid, so it never starts. One daemon per shared
## home would also be shared across sandboxes. Nothing in a one-TUI sandbox needs it.
cat > /etc/codex/config.toml << EOF
cli_auth_credentials_store = "file"
developer_instructions = "/etc/codex/AGENTS.md"
projects."$LOCKI_WORKTREES_HOME".trust_level = "trusted"

[features]
daemon_auto_start = false
EOF

# MARK: libatomic
## Node 25+ needs it and distros rarely ship it; the VM provides one with the sandbox tools

if ! ldconfig -p 2>/dev/null | grep -q libatomic; then
  mkdir -p /etc/ld.so.conf.d
  echo /usr/local/share/mise/locki/lib > /etc/ld.so.conf.d/locki.conf
  ldconfig 2>/dev/null || true
fi

# MARK: Sandbox tools
## Nothing to install: AI harnesses, CLIs, node and mise itself come from the VM, mounted read-only
## at /usr/local/share/mise (mise's default system data dir) and listed at their exact versions in
## the mise system config (MISE_SYSTEM_CONFIG_FILE, see services/tools.py). A repo's pins resolve
## to those installs when they match, and install into the sandbox (/usr/share/mise) when they don't.

## No shims: every bash loads the mise environment of its working directory, agents' command shells
## through BASH_ENV (non-interactive), login and interactive ones through profile.d, and the entry
## command runs under one too (ContainerService.exec_interactive). That puts the repo's pins, then
## Locki's tools, on PATH, behind /opt/locki/bin/high (the `_.path` of the system config), and
## `mise activate` follows `cd` and config changes. A pinned version missing everywhere installs
## on first use, through mise's command-not-found handler (not_found_auto_install).
## /etc/profile of some distros resets PATH: a login shell restores the Locki entries first.
## Locki's own bash shims skip all this: they only look up the real binary.
cat > /etc/profile.d/locki-mise.sh << 'EOF'
case "$0" in /opt/locki/bin/*) ;; *)
  case ":$PATH:" in *:/opt/locki/bin/high:*) ;; *)
    PATH="/opt/locki/bin/high:/root/.local/bin:$PATH:/usr/local/share/mise/locki/bin:/opt/locki/bin/low" ;;
  esac
  if [ -n "${BASH_VERSION:-}" ] && [ -z "${__LOCKI_MISE_ACTIVE:-}" ] && command -v mise >/dev/null 2>&1; then
    __LOCKI_MISE_ACTIVE=1
    eval "$(mise activate bash)"
  fi
esac
EOF

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
# flock is not reentrant: an install command may invoke another shim that auto-installs (e.g. pnpm),
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

## locki-fetch <url> <dest>: download with curl -> wget -> python3 fallback
cat > /opt/locki/bin/high/locki-fetch << 'EOF'
#!/bin/sh
if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 -o "$2" "$1"
elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
elif command -v python3 >/dev/null 2>&1; then python3 -c 'import sys
from urllib.request import urlretrieve, install_opener, build_opener
o = build_opener(); o.addheaders = [("User-Agent", "curl/8")]; install_opener(o)
urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
else echo "[Locki] Error: no HTTP client found (need curl, wget, or python3)" >&2; exit 1; fi
EOF

## locki-command-real: the binary a Locki shim stands in for. The mise version first (a repo pin,
## else Locki's tool), else PATH without Locki's own shims (/opt/locki/bin/*).
cat > /opt/locki/bin/high/locki-command-real << 'EOF'
#!/bin/sh
mise which "$1" 2>/dev/null || PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/opt/locki/bin/' | paste -sd:) command -v "$1" || exit 1
EOF

## locki-node-modules-redirect: point the project's node_modules at the btrfs cache
## (under scoped/<sandbox-id>/ so sandbox removal can delete it generically).
## Skips when there is no package.json (don't litter arbitrary cwds) or when a real
## node_modules directory already exists (respect it instead of nesting a junk symlink).
cat > /opt/locki/bin/high/locki-node-modules-redirect << 'EOF'
#!/bin/sh
[ -n "${LOCKI_SCOPED_CACHE:-}" ] || exit 0
_dir="$("$(locki-command-real npm)" prefix 2>/dev/null)" || exit 0
[ -f "$_dir/package.json" ] || exit 0
_target="$LOCKI_SCOPED_CACHE/node-modules${_dir}/node_modules"
if [ -L "$_dir/node_modules" ] || [ ! -e "$_dir/node_modules" ]; then
  mkdir -p "$_target" 2>/dev/null || exit 0
  ln -sfn "$_target" "$_dir/node_modules" 2>/dev/null || true
fi
exit 0
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
exec "$(locki-command-real "$cmd")" "$@"
EOF

## agent-browser: install chromium if missing, set env, then exec real binary
cat > /opt/locki/bin/high/agent-browser << 'EOF'
#!/bin/bash
set -eo pipefail
if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install chromium sh -c 'if command -v dnf >/dev/null 2>&1; then dnf install -yq chromium; elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -yqq chromium-browser; fi'
fi
export AGENT_BROWSER_EXECUTABLE_PATH=$(command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null)
exec "$(locki-command-real agent-browser)" "$@"
EOF

## copilot: --yolo has no full config equivalent (defaultPermissionMode skips resumed sessions,
## COPILOT_ALLOW_ALL skips paths/URLs); copilot tolerates the repeats from older ai_command strings
cat > /opt/locki/bin/high/copilot << 'EOF'
#!/bin/sh
exec "$(locki-command-real copilot)" --yolo --no-auto-update "$@"
EOF

## npm: symlink node_modules to btrfs
cat > /opt/locki/bin/high/npm << 'EOF'
#!/bin/bash
set -eo pipefail
locki-node-modules-redirect
exec "$(locki-command-real npm)" "$@"
EOF

## pnpm: cache + global virtual store
cat > /opt/locki/bin/high/pnpm << 'EOF'
#!/bin/bash
set -eo pipefail
_real=$(locki-command-real pnpm) || exit 1
if ! "$_real" config get enable-global-virtual-store 2>/dev/null | grep -q true; then
  /opt/locki/bin/high/locki-auto-install pnpm sh -c "\"$_real\" config set store-dir /var/cache/locki/pnpm && \"$_real\" config set global-bin-dir /usr/local/bin && \"$_real\" config set enable-global-virtual-store true && \"$_real\" config delete virtual-store-dir 2>/dev/null || true"
fi
exec "$_real" "$@"
EOF

## uv: symlink .venv to btrfs, scoped per sandbox (skip if a real .venv directory already exists)
cat > /opt/locki/bin/high/uv << 'EOF'
#!/bin/bash
set -eo pipefail
_real=$(locki-command-real uv) || exit 1
if [ -n "${LOCKI_SCOPED_CACHE:-}" ] && _dir="$("$_real" workspace dir 2>/dev/null)"; then
  if [ -L "$_dir/.venv" ] || [ ! -e "$_dir/.venv" ]; then
    export UV_PROJECT_ENVIRONMENT="$LOCKI_SCOPED_CACHE/uv-venvs${_dir}/.venv"
    ln -sfn "$UV_PROJECT_ENVIRONMENT" "$_dir/.venv" 2>/dev/null || true
  fi
fi
exec "$_real" "$@"
EOF

## yarn: symlink node_modules to btrfs
cat > /opt/locki/bin/high/yarn << 'EOF'
#!/bin/bash
set -eo pipefail
locki-node-modules-redirect
exec "$(locki-command-real yarn)" "$@"
EOF

## bun: symlink node_modules to btrfs + redirect cache (BUN_INSTALL_CACHE_DIR)
cat > /opt/locki/bin/high/bun << 'EOF'
#!/bin/bash
set -eo pipefail
locki-node-modules-redirect
exec "$(locki-command-real bun)" "$@"
EOF

## Docker: auto-install + route builds through the shared BuildKit daemon.
## Must live in high (shadowing the real docker) — otherwise the build redirect
## stops working the moment moby-engine lands in /usr/sbin.
cat > /opt/locki/bin/high/docker << 'EOF'
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

[ -n "${LOCKI_INSTALLING:-}" ] || flock /var/cache/locki/.install.lock true 2>/dev/null || true

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

  ## FROM refs present in this sandbox's dockerd are invisible to the shared
  ## buildkitd, which pulls from the registry instead — a hard failure for
  ## locally-built images. Ship each locally-present ref as an oci-layout build
  ## context so local images win, matching plain `docker build` semantics.
  ## Best-effort: any failure means an unpinned build.
  dockerfile= ctx= prev=
  extra=() dfargs=()
  for a in "$@"; do
    if [ -n "$prev" ]; then
      case "$prev" in -f|--file) dockerfile=$a ;; --build-arg) dfargs+=(-build-arg "$a") ;; esac
      prev=
      continue
    fi
    case "$a" in
      --file=*) dockerfile=${a#*=} ;;
      --build-arg=*) dfargs+=(-build-arg "${a#*=}") ;;
      --load|--push|--pull|--no-cache|--rm|--force-rm|--squash|--compress|-q|--quiet|-D|--debug|--check|--detach) ;;
      -*=*) ;;
      -*) prev=$a ;;  # assume unknown flags take a value; a misparse just skips pinning
      *) ctx=$a ;;
    esac
  done
  if [ -d "$ctx" ] && [ -n "${LOCKI_SCOPED_CACHE:-}" ]; then
    pindir="$LOCKI_SCOPED_CACHE/oci-pin"
    while IFS= read -r ref; do
      id=$("$_real" image inspect -f '{{.Id}}' "$ref" 2>/dev/null) || continue
      dir="$pindir/$(printf %s "$ref" | sha256sum | cut -d' ' -f1)"
      if [ "$(cat "$dir/locki-id" 2>/dev/null)" != "$id" ]; then
        # full re-export per new image ID; upgrade path: read dockerd's containerd store directly
        mkdir -p "$pindir"
        tmp=$(mktemp -d "$pindir/.pin-XXXXXX") || continue
        if "$_real" save "$ref" | tar -xf - -C "$tmp" && printf %s "$id" > "$tmp/locki-id"; then
          rm -rf "$dir"
          mv "$tmp" "$dir" 2>/dev/null || rm -rf "$tmp"  # lost a concurrent export race: keep winner's copy
        else
          rm -rf "$tmp"
          continue
        fi
      fi
      digest=$(jq -r '.manifests[0].digest // empty' "$dir/index.json" 2>/dev/null) || true
      [ -n "$digest" ] && extra+=(--build-context "$ref=oci-layout://$dir@$digest")
    done < <(dockerfile-json -quiet "${dfargs[@]}" "${dockerfile:-$ctx/Dockerfile}" 2>/dev/null \
      | jq -r '[.Stages[].Name | select(. != "") | ascii_downcase] as $stages
          | [(.Stages[].From.Image // empty), (.Stages[].Commands[]? | (.From // empty), (.Mounts[]?.From // empty))]
          | unique[]
          | select(. != "" and (test("^[0-9]+$") | not) and (ascii_downcase as $l | $stages | index($l) | not))' 2>/dev/null)
  fi

  has_output=
  for a in "$@"; do case "$a" in --load|--push|--output*|-o*) has_output=1 ;; esac; done
  set -- buildx build --builder locki "${extra[@]}" "$@"
  [ -z "$has_output" ] && set -- "$@" --load
fi
exec "$_real" "$@"
EOF

chmod +x /opt/locki/bin/high/*

# MARK: Low-priority shims

mkdir -p /opt/locki/bin/low

## pnpm, pnpx, yarn and yarnpkg come with corepack (sandbox tools); pnpm's newer short
## alias for pnpx does not
cat > /opt/locki/bin/low/pnx << 'EOF'
#!/bin/sh
exec pnpx "$@"
EOF

## The release tarball's only binary is `antigravity`; `agy` (the name upstream's own
## installer uses, and what users type) is a symlink shipped in the macOS archive only.
cat > /opt/locki/bin/low/agy << 'EOF'
#!/bin/sh
exec antigravity "$@"
EOF

cat > /opt/locki/bin/low/bwrap << 'EOF'
#!/bin/sh
real=$(locki-command-real bwrap 2>/dev/null) && exec "$real" "$@"
while [ "$#" -gt 0 ]; do [ "$1" = "--" ] && { shift; break; }; shift; done
[ "$#" -gt 0 ] && exec "$@"
exit 0
EOF

chmod +x /opt/locki/bin/low/*

# MARK: Caching

if command -v apt-get >/dev/null 2>&1; then
  ## Share only the caches (archives + repo metadata); the rest of Dir::State
  ## (e.g. extended_states auto-install markers) is per-container state.
  mkdir -p /etc/apt/apt.conf.d /var/cache/locki/apt/cache/archives/partial /var/cache/locki/apt/lists/partial
  printf 'Dir::Cache "/var/cache/locki/apt/cache";\nDir::State::lists "/var/cache/locki/apt/lists";\n' > /etc/apt/apt.conf.d/99local-cache
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
if /opt/locki/bin/high/locki-fetch "$ca_url" "$ca_tmp"; then
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
    echo '10.99.0.1 __INTERCEPTED_HOSTS__' >> /etc/hosts
  fi
fi
rm -f "$ca_tmp"
