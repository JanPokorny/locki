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
#   - old tool versions pruned once nothing uses them any more
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
rc=0
printf '%s\n' "$token" | runuser -u "$USER" -- env -i \
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

## Exact versions, not the `latest` links of mise: some bin folders are named after the version
## (ripgrep-<version>-<target>), and a running session keeps its PATH until it exits. The
## pruning below keeps the old versions for exactly as long as such sessions live.
{
  printf ":%s" "$mise_dir"
  mise bin-paths | while IFS= read -r dir; do printf ":%s" "$(readlink -f "$dir")"; done
} > "$ROOT/path.new" || rc=1
mv "$ROOT/path.new" "$ROOT/path"
exit "$rc"
' || rc=$?

# MARK: As root: prune versions nothing uses

## Sandboxes are containers in this VM, so /proc here lists every sandbox process. A tool
## version is in use while any process runs it (exe), maps its libraries (maps), runs a
## script of it (cmdline) or has it on its PATH (environ: a session started before an
## upgrade keeps the old PATH, and so does everything it launches). Another install can
## depend on it too: a pipx venv (poetry) links to its python and records it as `home`.
## Links within a version, like fd and uv shipping links to themselves, do not count.
## Installs change only under the tools lock (services/tools.py), so nothing races this.
installs="$ROOT/mise/installs"
seg="[^/:[:cntrl:][:space:]]*"
vdir() { printf "%s\n" "$1" | grep -o "^$installs/$seg/$seg" || true; }
in_use=$(mktemp)
{
  tr ":" "\n" < "$ROOT/path" || true
  find /proc -mindepth 2 -maxdepth 2 -name exe -printf "%l\n" 2>/dev/null || true
  grep -aho "$installs/$seg/$seg" /proc/[0-9]*/maps /proc/[0-9]*/cmdline /proc/[0-9]*/environ 2>/dev/null || true
  find "$installs" -type l -lname "$installs/*" 2>/dev/null | while IFS= read -r link; do
    target=$(readlink -f "$link") || continue
    [ "$(vdir "$link")" = "$(vdir "$target")" ] || printf "%s\n" "$target"
  done
  find "$installs" -name pyvenv.cfg -exec sed -n "s/^home *= *//p" {} + 2>/dev/null || true
} | grep -o "$installs/$seg/$seg" | sort -u > "$in_use" || true

for tool in "$installs"/*/; do
  [ -d "$tool" ] || continue
  versions=$(find "$tool" -mindepth 1 -maxdepth 1 -type d -printf "%f\n" | sort -V)
  newest=$(printf "%s\n" "$versions" | tail -n 1)
  printf "%s\n" "$versions" | while IFS= read -r v; do
    [ -n "$v" ] && [ "$v" != "$newest" ] || continue
    grep -qxF "$tool$v" "$in_use" || rm -rf "$tool$v"
  done
  find "$tool" -mindepth 1 -maxdepth 1 -xtype l -delete
done
rm -f "$in_use"
exit "$rc"
