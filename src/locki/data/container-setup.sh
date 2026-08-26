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

# MARK: mise lockfile
## Pinned resolutions for every tool the shims install. Only locki-mise-install reads it,
## and only after a normal install fails — typically because api.github.com's 60/hr
## anonymous limit, shared by every sandbox behind the VM's IP, is exhausted. Regenerate
## with `mise run lock-tools`; versions are as old as the last regeneration.

mkdir -p /opt/locki
set +x
echo '__MISE_LOCK_B64__' | base64 -d > /opt/locki/mise.lock
set -x

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

## locki-mise-install: install a package globally via mise (shim-safe env)
cat > /opt/locki/bin/high/locki-mise-install << 'EOF'
#!/bin/sh
# MISE_LOCKFILE=false so installs resolve current versions and ignore /opt/locki/mise.lock.
# Scoped to this script: setting it in the container env would make mise ignore the
# lockfiles of the repos worked on in the sandbox.
export MISE_AUTO_INSTALL=false MISE_NO_HOOKS=true MISE_LOCKFILE=false
mise use -g "$1" && mise install "$1" && exit 0
# Version resolution goes through api.github.com, so it dies once that anonymous rate
# limit is spent. Fall back to the shipped lockfile: MISE_LOCKED demands its pre-resolved
# URLs, needing no API at all, at the cost of a possibly stale version.
printf '\033[1;33mᛚ\033[0m Retrying %s from Locki'"'"'s pinned lockfile\n' "$1" >&2
export MISE_LOCKFILE=true MISE_LOCKED=true
mise use -g "$1" && mise install "$1"
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

## locki-command-real: resolve binary outside ALL /opt/locki/bin/ shim folders
## MISE_LOCKFILE=false to match locki-mise-install: installs resolve current versions, so a
## lockfile lagging upstream would pin `which` to a version that was never installed.
cat > /opt/locki/bin/high/locki-command-real << 'EOF'
#!/bin/sh
_mise="${MISE_INSTALL_PATH:-/usr/local/bin/mise}"
PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v -e '^/opt/locki/bin/' -e '/mise/shims' | paste -sd:) command -v "$1" || MISE_LOCKFILE=false "$_mise" which "$1" 2>/dev/null || exit 1
EOF

## locki-command-real-or-autoinstalled: resolve binary outside /opt/locki/bin/high (low shims still reachable)
cat > /opt/locki/bin/high/locki-command-real-or-autoinstalled << 'EOF'
#!/bin/sh
_mise="${MISE_INSTALL_PATH:-/usr/local/bin/mise}"
MISE_LOCKFILE=false "$_mise" which "$1" 2>/dev/null || PATH=$(printf '%s' "${PATH#*/opt/locki/bin/high:}" | tr ':' '\n' | grep -v '/mise/shims' | paste -sd:) command -v "$1" || exit 1
EOF

## locki-node-modules-redirect: point the project's node_modules at the btrfs cache
## (under scoped/<sandbox-id>/ so sandbox removal can delete it generically).
## Skips when there is no package.json (don't litter arbitrary cwds) or when a real
## node_modules directory already exists (respect it instead of nesting a junk symlink).
cat > /opt/locki/bin/high/locki-node-modules-redirect << 'EOF'
#!/bin/sh
[ -n "${LOCKI_SCOPED_CACHE:-}" ] || exit 0
_dir="$("$(locki-command-real-or-autoinstalled npm)" prefix 2>/dev/null)" || exit 0
[ -f "$_dir/package.json" ] || exit 0
_target="$LOCKI_SCOPED_CACHE/node-modules${_dir}/node_modules"
if [ -L "$_dir/node_modules" ] || [ ! -e "$_dir/node_modules" ]; then
  mkdir -p "$_target" 2>/dev/null || exit 0
  ln -sfn "$_target" "$_dir/node_modules" 2>/dev/null || true
fi
exit 0
EOF

cat > /opt/locki/bin/high/locki-ensure-node << 'EOF'
#!/bin/sh
locki-command-real node >/dev/null 2>&1 && exit 0
# mise resolves npm-backed tools (npm:foo) by shelling out to `npm`, which lands back on the
# npm shim while node is still missing -> unbounded recursion. Only the outermost call installs.
[ -z "${LOCKI_ENSURING_NODE:-}" ] || exit 0
export LOCKI_ENSURING_NODE=1
/opt/locki/bin/high/locki-auto-install nodejs /opt/locki/bin/high/locki-mise-install node >/dev/null 2>&1
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

## node/npx: install Node.js if missing via mise
for bin in node npx; do
  cat > "/opt/locki/bin/high/$bin" << EOF
#!/bin/bash
set -eo pipefail
locki-ensure-node
exec "\$(locki-command-real-or-autoinstalled $bin)" "\$@"
EOF
done

## npm: install Node.js if missing + symlink node_modules to btrfs
cat > /opt/locki/bin/high/npm << 'EOF'
#!/bin/bash
set -eo pipefail
locki-ensure-node
locki-node-modules-redirect
exec "$(locki-command-real-or-autoinstalled npm)" "$@"
EOF

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

## uv: symlink .venv to btrfs, scoped per sandbox (skip if a real .venv directory already exists)
cat > /opt/locki/bin/high/uv << 'EOF'
#!/bin/bash
set -eo pipefail
_real=$(locki-command-real-or-autoinstalled uv) || exit 1
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
exec "$(locki-command-real-or-autoinstalled yarn)" "$@"
EOF

## bun: symlink node_modules to btrfs + redirect cache (BUN_INSTALL_CACHE_DIR)
cat > /opt/locki/bin/high/bun << 'EOF'
#!/bin/bash
set -eo pipefail
locki-node-modules-redirect
exec "$(locki-command-real-or-autoinstalled bun)" "$@"
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
    locki-ensure-node
    /opt/locki/bin/high/locki-auto-install @anthropic-ai/claude-code /opt/locki/bin/high/locki-mise-install npm:@anthropic-ai/claude-code
  fi
fi
## Claude Code plugins run their hooks with `node` under short per-hook timeouts (~5s).
## The RPM claude brings no node, so on a fresh home the first hooks -- including
## one-shot SessionStart ones -- land on the node shim's install and are killed
## mid-download together with the hook. Install node up front instead: visible and
## blocking, like any other shim, so hooks work from the first session.
if ! locki-command-real node >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install nodejs /opt/locki/bin/high/locki-mise-install node
fi
exec "$(locki-command-real claude)" "$@"
EOF

## NPM packages
for pair in \
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
  locki-ensure-node
  /opt/locki/bin/high/locki-auto-install $pkg /opt/locki/bin/high/locki-mise-install npm:$pkg
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
  "github:google-antigravity/antigravity-cli=antigravity" \
  "github:github/copilot-cli=copilot" \
  "github:keilerkonzept/dockerfile-json=dockerfile-json" \
  "jq=jq" \
  "k9s=k9s" \
  "kubectl=kubectl" \
  "pipx:poetry=poetry" \
  "python=pip" \
  "python=pip3" \
  "python=python" \
  "python=python3" \
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
  /opt/locki/bin/high/locki-auto-install $pkg /opt/locki/bin/high/locki-mise-install $pkg
fi
exec "\$(locki-command-real $bin)" "\$@"
EOF
done

## The release tarball's only binary is `antigravity`; `agy` (the name upstream's own
## installer uses, and what users type) is a symlink shipped in the macOS archive only.
ln -sf antigravity /opt/locki/bin/low/agy

cat > /opt/locki/bin/low/bwrap << 'EOF'
#!/bin/sh
real=$(locki-command-real bwrap 2>/dev/null) && exec "$real" "$@"
while [ "$#" -gt 0 ]; do [ "$1" = "--" ] && { shift; break; }; shift; done
[ "$#" -gt 0 ] && exec "$@"
exit 0
EOF

## Mise
cat > /opt/locki/bin/low/mise << 'EOF'
#!/bin/bash
set -eo pipefail
if ! locki-command-real mise >/dev/null 2>&1; then
  /opt/locki/bin/high/locki-auto-install mise sh -c '
    set -eu
    # >=2026.5 for MISE_PROVENANCE_API_FAILURES_FATAL: older mise ignores it and the
    # lockfile fallback dies verifying provenance against the API it is working around.
    mise_version="2026.7.15"
    musl=""; if ldd /bin/ls 2>/dev/null | grep musl; then musl="-musl"; fi
    case "$(uname -m)" in x86_64) arch="x64$musl";; aarch64|arm64) arch="arm64$musl";; esac
    dest="/var/cache/locki/mise-install/mise-v${mise_version}-linux-${arch}"
    if ! test -x "$dest/mise/bin/mise"; then
      ext="tar.gz"
      if command -v zstd >/dev/null 2>&1 && tar --version 2>/dev/null | grep -q "1\.\(3[1-9]\|[4-9][0-9]\)"; then ext="tar.zst"; fi
      case "$arch.$ext" in
        x64.tar.gz)         checksum="0785821a617e85197104c021835072ca3f4fcdda143538293a30593acc258969";;
        x64-musl.tar.gz)    checksum="4ed34fb8af855de81504bc669c95bdd31966a43418f35829f240d96faf6d89b7";;
        arm64.tar.gz)       checksum="0c2ca4d4ee79720a08d2c5f54c986450348b0fe25ace2bf9998dbe6c6761bf16";;
        arm64-musl.tar.gz)  checksum="6067a008b6e87ca9c50a63a1c38cbc9ae478191f92f511ea71aa8e6108832205";;
        x64.tar.zst)        checksum="78a67a8a7edc5292cc74d2ac6c160cb2936b09e8bdbb327804bcb2b6afae8e02";;
        x64-musl.tar.zst)   checksum="000d4410432f58b9398ba3f6796ca23ad285e0a222e0d19c93a78f2e30cdc608";;
        arm64.tar.zst)      checksum="192ff3d6d07b772592cbce7103187f6508fed7207c7ac6d351642c0f3a8b995b";;
        arm64-musl.tar.zst) checksum="349a1a6cfae38a22dd5096ce8b8ab27d869babde7ceafbfe9b8de9154a84bcb8";;
        *) echo "no checksum for linux-$arch.$ext" >&2; exit 1;;
      esac
      tmpdir=$(mktemp -d)
      unpack=""
      trap "rm -rf \"\$tmpdir\" \"\$unpack\"" EXIT
      mise_file="mise-v$mise_version-linux-$arch.$ext"
      mise_url="https://mise.jdx.dev/v$mise_version/$mise_file"
      /opt/locki/bin/high/locki-fetch "$mise_url" "$tmpdir/$mise_file"
      if [ "$(sha256sum "$tmpdir/$mise_file" | cut -d" " -f1)" != "$checksum" ]; then echo "checksum mismatch" >&2; exit 1; fi
      # Extract into a temp dir and rename: the cache is shared across sandboxes, so a
      # crashed extraction must not leave a half-populated dir that later passes the check.
      mkdir -p /var/cache/locki/mise-install
      unpack=$(mktemp -d /var/cache/locki/mise-install/.unpack-XXXXXX)
      cd "$unpack"
      if [ "$ext" = "tar.zst" ]; then zstd -d -c "$tmpdir/$mise_file" | tar -xf -; else tar -xf "$tmpdir/$mise_file"; fi
      cd /
      rm -rf "$dest"
      mv "$unpack" "$dest"
    fi
    chmod +x "$dest/mise/bin/mise"
    ln -sf "$dest/mise/bin/mise" /usr/local/bin/mise
    chmod +x /usr/local/bin/mise
  '
fi
exec "$(locki-command-real mise)" "$@"
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
