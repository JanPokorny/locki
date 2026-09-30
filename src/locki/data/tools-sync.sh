#!/bin/sh
# Sandbox tools: installed once in the VM, mounted read-only into every sandbox.
#
# Runs in the VM as root (see services/tools.py). Idempotent; every run converges the VM:
#   - mise itself (pinned below) under $ROOT/mise-bin/<version>/
#   - every tool of $ROOT/mise.toml installed under $ROOT/mise/ ($2=upgrade also moves
#     `latest` tools to their newest release)
#   - $ROOT/path: the tools' bin folders (`mise bin-paths`, resolved to exact versions) as a
#     `:<dir>...` PATH suffix, replaced atomically; sandboxes read it on entry
#   - the `locki-tools` incus profile device mounting $ROOT read-only in every sandbox
#
# Args: $1 mise.toml (base64), $2 "upgrade" or "".
# Stdin: a GitHub token line, possibly empty. It is only ever held in the environment
# of the unprivileged mise run below: never written to disk, never seen by a sandbox.
set -eu

ROOT=/var/lib/locki/tools
CACHE=/var/lib/locki/tools-cache
USER=locki-tools

MISE_VERSION="2026.9.17"
case "$(uname -m)" in
  x86_64)  arch="x64";   checksum="8d1bcbc0b2ba167ee765e7410502c3f89974d0195eb8ec74537bc93bb367420d";;
  aarch64) arch="arm64"; checksum="46717187f93d4ebfff8b87a30da3f5939af995057c0c1a855e968f0ee4d19c88";;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1;;
esac

IFS= read -r token || true
config=$1 mode=$2

# MARK: As root: system prerequisites

## Node 25+ needs libatomic, which the Lima Fedora image may lack
rpm -q libatomic >/dev/null 2>&1 || dnf install -y -q --setopt install_weak_deps=False libatomic

## Installs run unprivileged: npm/pipx install scripts must not run as VM root
id "$USER" >/dev/null 2>&1 || useradd --system --user-group --home-dir "$CACHE" --shell /sbin/nologin "$USER"
mkdir -p "$ROOT" "$CACHE"
chown "$USER:$USER" "$ROOT" "$CACHE"
chmod 755 "$ROOT"

if command -v incus >/dev/null 2>&1 && ! incus profile device get default locki-tools path >/dev/null 2>&1; then
  incus profile device add default locki-tools disk source="$ROOT" path="$ROOT" readonly=true \
    || echo "Could not mount the sandbox tools into sandboxes (incus profile device add failed)" >&2
fi

# MARK: As $USER: mise, tools, PATH

printf '%s' "$config" | base64 -d > "$CACHE/mise.toml.new"
chown "$USER:$USER" "$CACHE/mise.toml.new"

## The token goes through a pipe: in argv or a file, other VM processes could read it
cd /
printf '%s\n' "$token" | exec runuser -u "$USER" -- env -i \
  HOME="$CACHE" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
  ROOT="$ROOT" CACHE="$CACHE" MISE_VERSION="$MISE_VERSION" ARCH="$arch" CHECKSUM="$checksum" MODE="$mode" \
  sh -eu -c '
IFS= read -r t || true
[ -z "$t" ] || export MISE_GITHUB_TOKEN="$t"
unset t
mise_dir="$ROOT/mise-bin/$MISE_VERSION"
if ! test -x "$mise_dir/mise"; then
  tmp=$(mktemp -d "$CACHE/.mise-XXXXXX")
  trap "rm -rf $tmp" EXIT
  curl -fsSL --retry 3 -o "$tmp/mise.tar.gz" "https://mise.jdx.dev/v$MISE_VERSION/mise-v$MISE_VERSION-linux-$ARCH.tar.gz"
  [ "$(sha256sum "$tmp/mise.tar.gz" | cut -d" " -f1)" = "$CHECKSUM" ] || { echo "mise checksum mismatch" >&2; exit 1; }
  tar -xzf "$tmp/mise.tar.gz" -C "$tmp"
  mkdir -p "$ROOT/mise-bin"
  rm -rf "$mise_dir" && mv "$tmp/mise/bin" "$mise_dir"
fi

mv "$CACHE/mise.toml.new" "$ROOT/mise.toml"
export PATH="$mise_dir:$PATH"
export MISE_DATA_DIR="$ROOT/mise" MISE_CACHE_DIR="$CACHE/mise" MISE_STATE_DIR="$CACHE/mise-state" \
  MISE_CONFIG_DIR="$CACHE/mise-config" MISE_GLOBAL_CONFIG_FILE="$ROOT/mise.toml" MISE_SYSTEM_CONFIG_FILE="$CACHE/no-system-config.toml" \
  MISE_YES=1 MISE_NODE_VERIFY=false MISE_PROVENANCE_API_FAILURES_FATAL=false \
  UV_PYTHON_INSTALL_DIR="$ROOT/uv-python" UV_CACHE_DIR="$CACHE/uv" UV_SYSTEM_CERTS=1 npm_config_cache="$CACHE/npm"

## One failing tool must not keep the others from installing or upgrading
rc=0
mise install || rc=1
if [ "$MODE" = upgrade ]; then mise upgrade || rc=1; fi

## Old versions stay for sessions still running them; keep the newest two per tool.
## Never python: pipx tools (poetry) run from venvs linked to the version they were built with.
for tool in "$MISE_DATA_DIR"/installs/*/; do
  case "$tool" in */installs/python/) continue;; esac
  [ -d "$tool" ] || continue
  find "$tool" -mindepth 1 -maxdepth 1 -type d -printf "%f\n" | sort -V | head -n -2 | while IFS= read -r v; do
    rm -rf "$tool$v"
  done
  find "$tool" -mindepth 1 -maxdepth 1 -xtype l -delete
done

## Exact versions, not the `latest` links of mise: some bin folders are named after the version
## (ripgrep-<version>-<target>), and a running session keeps its PATH until the next entry.
## The pruning above keeps the previous version for exactly those sessions.
{
  printf ":%s" "$mise_dir"
  mise bin-paths | while IFS= read -r dir; do printf ":%s" "$(readlink -f "$dir")"; done
} > "$ROOT/path.new" || rc=1
mv "$ROOT/path.new" "$ROOT/path"
exit "$rc"
'
