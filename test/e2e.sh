#!/bin/bash
set -euo pipefail

# ── helpers ──────────────────────────────────────────────────────────────────

PASS=0
FAIL=0
ERRORS=""

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); ERRORS="$ERRORS\n  ✗ $1"; }

assert_ok() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

assert_fail() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then fail "$desc"; else pass "$desc"; fi
}

assert_output() {
    local desc="$1" expected="$2"; shift 2
    local actual stderr_file
    stderr_file=$(mktemp)
    actual=$("$@" 2>"$stderr_file") || true
    if [[ "$actual" == *"$expected"* ]]; then pass "$desc"; else fail "$desc (expected '$expected', got '$actual')"; cat "$stderr_file" >&2; fi
    rm -f "$stderr_file"
}

timed() {
    local start end
    start=$(date +%s)
    "$@" >/dev/null
    end=$(date +%s)
    echo $((end - start))
}

# ── setup ────────────────────────────────────────────────────────────────────

# Use /tmp directly to keep paths short — Lima needs UNIX_PATH_MAX < 104 for sockets
# Resolve symlinks (macOS: /tmp -> /private/tmp) to avoid path mismatches
TMPDIR_ROOT=$(cd "$(mktemp -d /tmp/locki-e2e.XXXX)" && pwd -P)
export HOME="$TMPDIR_ROOT/h"
mkdir -p "$HOME"
export XDG_CONFIG_HOME="$TMPDIR_ROOT/xdg/config"
export XDG_DATA_HOME="$TMPDIR_ROOT/xdg/data"
export XDG_STATE_HOME="$TMPDIR_ROOT/xdg/state"
export XDG_RUNTIME_DIR="$TMPDIR_ROOT/xdg/run"
export LIMA_HOME="$XDG_STATE_HOME/locki/lima"
kill_locki_sshd() { local pf="$XDG_RUNTIME_DIR/locki/sshd.pid"; [ -f "$pf" ] && kill "$(cat "$pf")" 2>/dev/null || true; }
kill_locki_sshd
cleanup() { kill_locki_sshd; limactl delete -f locki 2>/dev/null || true; rm -rf "$TMPDIR_ROOT"; }
trap cleanup EXIT

VENV="$TMPDIR_ROOT/v"
REPO="$TMPDIR_ROOT/r"
PROJECT_ROOT="$(cd "$(dirname "$0")/.."; pwd)"

json_field() { yq -r ".$1"; }
new_sandbox_id() { locki new --json 2>/dev/null | json_field id; }

echo "Setting up venv and installing locki..."
uv venv "$VENV" --python 3.14
export PATH="$VENV/bin:$PATH"
uv pip install --python "$VENV/bin/python" "$PROJECT_ROOT"

REMOTE="$TMPDIR_ROOT/my_repo.v2-test"

echo "Creating test repo..."
git init --bare "$REMOTE"
git clone "$REMOTE" "$REPO"
git -C "$REPO" config user.name "Locki Test"
git -C "$REPO" config user.email "locki@example.com"
git -C "$REPO" commit --allow-empty -m "initial"
git -C "$REPO" push

cd "$REPO"

locki setup --defaults

# ── cold start + parallel VM creation ────────────────────────────────────────

echo
echo "Testing cold start + parallel VM creation..."

AUTH=$(new_sandbox_id)
cold_start=$(timed locki x -m "$AUTH" echo 1) || true
echo "  cold start: ${cold_start}s"

# branch b in parallel with a (VM already exists, but tests lock waiting)
LOGIN=$(new_sandbox_id)
assert_output "locki x b runs" "2" locki x -m "$LOGIN" echo 2

# ── cache persistence across invocations ─────────────────────────────────────

echo
echo "Testing cache persistence..."

locki x -m "$AUTH" mkdir -p /var/cache/locki
assert_ok "write to cache" bash -c "echo 42 | locki x -m '$AUTH' tee /var/cache/locki/test >/dev/null"
assert_ok "cached file persists" locki x -m "$AUTH" test -f /var/cache/locki/test

# ── hook execution in guest ──────────────────────────────────────────────────

echo
echo "Testing hook execution in guest..."

HOOKS_DIR="$REPO/.git/hooks"
mkdir -p "$HOOKS_DIR"
WORKTREE=$(git worktree list --porcelain | grep -B2 "branch refs/heads/untitled#locki-$AUTH" | head -1 | sed 's/worktree //')

cat > "$HOOKS_DIR/pre-commit" << HOOK
#!/bin/bash
set -e
# This file only exists inside the guest container's cache — not on host
cp /var/cache/locki/test $WORKTREE/hook-proof
HOOK
chmod +x "$HOOKS_DIR/pre-commit"

git -C "$WORKTREE" commit --allow-empty -m "trigger hook" 2>/dev/null || true
assert_ok "hook created file from guest" test -f "$WORKTREE/hook-proof"
assert_output "hook copied correct content" "42" cat "$WORKTREE/hook-proof"

# ── proxied git/gh commands ──────────────────────────────────────────────────

echo
echo "Testing proxied git commands..."

assert_ok    "git status works"              locki x -m "$AUTH" git status
assert_ok    "git log works"                 locki x -m "$AUTH" git log --oneline
assert_ok    "git diff works"                locki x -m "$AUTH" git diff
assert_ok    "git show works"                locki x -m "$AUTH" git show
assert_fail  "git checkout is blocked"       locki x -m "$AUTH" git checkout main
assert_fail  "git reset --hard (no ref) is blocked" locki x -m "$AUTH" git reset --hard
assert_ok    "git reset <ref> --hard works"  locki x -m "$AUTH" git reset HEAD --hard

# Short-flag handling: registered aliases work in both `-x val` and `-xval` forms;
# unregistered shorts are rejected.
assert_ok    "known short flag works (-n 1)"    locki x -m "$AUTH" git log -n 1
assert_ok    "known short flag glued (-n1)"     locki x -m "$AUTH" git log -n1
assert_fail  "unknown short flag is blocked"    locki x -m "$AUTH" git log -z

# Conservative pairing: `-x` before a `-`-prefixed next arg must NOT pair.  Git
# would pair `-m --amend` (message="--amend"); we reject.  This keeps attackers
# from smuggling flags into value positions.
assert_fail  "-m does not pair with --amend"    locki x -m "$AUTH" git commit -m --amend
assert_fail  "--message does not pair with --amend" locki x -m "$AUTH" git commit --message --amend

# Pre-subcommand git flags (not in grammar) are rejected — no way to inject
# `-c alias=...`, `--git-dir=...`, etc.
assert_fail  "git -c config override blocked"   locki x -m "$AUTH" git -c alias.st=status status
assert_fail  "git --git-dir blocked"            locki x -m "$AUTH" git --git-dir=/tmp/evil status

# Stash: message must carry the sandbox suffix; pop/drop require an owned ref.
assert_fail  "stash push without suffix"        locki x -m "$AUTH" git stash push -m plain
assert_fail  "stash pop without ref"            locki x -m "$AUTH" git stash pop
assert_fail  "stash pop of non-owned ref"       locki x -m "$AUTH" git stash pop 'stash@{99}'

# ── git commit from sandbox ─────────────────────────────────────────────────

echo
echo "Testing git commit from sandbox..."

WORKTREE_A=$(git worktree list --porcelain | grep -B2 "branch refs/heads/untitled#locki-$AUTH" | head -1 | sed 's/worktree //')
echo test-content | locki x -m "$AUTH" tee "$WORKTREE_A/commit-test.txt" >/dev/null
locki x -m "$AUTH" git add --all
locki x -m "$AUTH" git commit --message='simple commit'
assert_output "simple commit landed" "simple commit" git -C "$WORKTREE_A" log -1 --format=%s

# Multi-line commit message (newlines triggered $'...' quoting bug)
echo more | locki x -m "$AUTH" tee "$WORKTREE_A/commit-test2.txt" >/dev/null
locki x -m "$AUTH" git add --all
locki x -m "$AUTH" git commit --message='multi line

second paragraph'
assert_output "multi-line commit subject" "multi line" git -C "$WORKTREE_A" log -1 --format=%s
assert_output "multi-line commit body" "second paragraph" git -C "$WORKTREE_A" log -1 --format=%b

# ── hook modifies COMMIT_EDITMSG ────────────────────────────────────────────

echo
echo "Testing commit-msg hook modifies message..."

cat > "$HOOKS_DIR/commit-msg" << 'HOOK'
#!/bin/bash
# Append a trailer to the commit message
echo "" >> "$1"
echo "Signed-off-by: Test Bot <test@example.com>" >> "$1"
HOOK
chmod +x "$HOOKS_DIR/commit-msg"

echo hook-msg-test | locki x -m "$AUTH" tee "$WORKTREE_A/hook-msg-file.txt" >/dev/null
locki x -m "$AUTH" git add --all
locki x -m "$AUTH" git commit --message='test hook message'
assert_output "commit-msg hook appended trailer" "Signed-off-by: Test Bot" git -C "$WORKTREE_A" log -1 --format=%b
assert_output "original message preserved" "test hook message" git -C "$WORKTREE_A" log -1 --format=%s

rm -f "$HOOKS_DIR/commit-msg"

# ── warm start (new container, existing VM) ──────────────────────────────────

echo
echo "Testing warm start..."

RELEASE=$(new_sandbox_id)
warm_start=$(timed locki x -m "$RELEASE" echo 3) || true
echo "  warm start: ${warm_start}s"

# ── hot start (existing container) ───────────────────────────────────────────

echo
echo "Testing hot start..."

hot_start=$(timed locki x -m "$RELEASE" echo 4) || true
echo "  hot start: ${hot_start}s"

# ── container isolation ──────────────────────────────────────────────────────

echo
echo "Testing container isolation..."

assert_ok "write secret in sandbox a" bash -c "echo secret | locki x -m '$AUTH' tee /tmp/a-only >/dev/null"
assert_fail "sandbox b can't see sandbox a's /tmp" locki x -m "$LOGIN" test -f /tmp/a-only

# ── custom image via locki.toml ──────────────────────────────────────────────

echo
echo "Testing locki.toml custom image..."

# String format (same image for all arches)
cat > "$REPO/locki.toml" << 'TOML'
incus_image = "images:ubuntu/24.04"
TOML

UBUNTU_SB=$(new_sandbox_id)
assert_output "string incus_image runs ubuntu" "Ubuntu" locki x -m "$UBUNTU_SB" cat /etc/os-release

# Legacy dict format (backward compat)
cat > "$REPO/locki.toml" << 'TOML'
[incus_image]
aarch64 = "images:ubuntu/24.04"
x86_64 = "images:ubuntu/24.04"
TOML

assert_output "dict incus_image runs ubuntu" "Ubuntu" locki x --new cat /etc/os-release

# Export Ubuntu image to test local file + glob (split format: metadata + .root)
LIMACTL=$(python -c 'from locki.utils import limactl; print(limactl())')
UBUNTU_FP=$("$LIMACTL" shell --start --workdir=/ locki -- sudo incus config get "$UBUNTU_SB" volatile.base_image)
"$LIMACTL" shell --start --workdir=/ locki -- sudo bash -c "
  set -e
  incus image export '$UBUNTU_FP' /tmp/locki-e2e-ubuntu-img
  # Delete cached image so re-import from local file doesn't conflict
  incus image delete '$UBUNTU_FP'
" >/dev/null
"$LIMACTL" copy locki:/tmp/locki-e2e-ubuntu-img "$TMPDIR_ROOT/ubuntu-img.tar.xz"
"$LIMACTL" copy locki:/tmp/locki-e2e-ubuntu-img.root "$TMPDIR_ROOT/ubuntu-img.tar.xz.root" 2>/dev/null || true
"$LIMACTL" shell --start --workdir=/ locki -- sudo rm -f /tmp/locki-e2e-ubuntu-img /tmp/locki-e2e-ubuntu-img.root >/dev/null

# Local file via string (no glob)
cat > "$REPO/locki.toml" << TOML
incus_image = "../ubuntu-img.tar.xz"
TOML

assert_output "local file string incus_image works" "Ubuntu" locki x --new cat /etc/os-release

# Glob with single match (pattern excludes the .root companion)
cat > "$REPO/locki.toml" << TOML
incus_image = "../ubuntu-img*.tar.xz"
TOML

assert_output "glob incus_image with single match works" "Ubuntu" locki x --new cat /etc/os-release

rm -f "$REPO/locki.toml"

# ── port forwarding ─────────────────────────────────────────────────────────

echo
echo "Testing port forwarding..."

# Install ncat in the container (base image doesn't include it)
locki x -m "$LOGIN" dnf install -y nmap-ncat

# Start a persistent listener inside the container
locki x -m "$LOGIN" bash -c "nohup bash -c 'while true; do echo pf-ok | ncat -l 9111; done' &>/dev/null &"

# Use a random host port to avoid conflicts with the user's main locki VM
pf_host_port=$(locki port-forward -m "$LOGIN" --json :9111 2>/dev/null | json_field '[0].host_port' || true)
if [[ -n "$pf_host_port" && "$pf_host_port" -ge 1024 ]]; then
    pass "port-forward assigns host port >= 1024"
else
    fail "port-forward assigns host port >= 1024 (got '$pf_host_port')"
fi

# Wait for Lima to detect and forward the new listening port
pf_ok=false
for i in $(seq 1 10); do
    if result=$(nc -4 -w2 127.0.0.1 "$pf_host_port" 2>/dev/null) && [[ "$result" == *"pf-ok"* ]]; then
        pf_ok=true; break
    fi
    sleep 1
done
if $pf_ok; then pass "port-forward is reachable"; else fail "port-forward is reachable (timed out after 10s)"; fi

assert_output "port-forward --list --json shows forward" "9111" bash -c "locki port-forward -m '$LOGIN' --list --json 2>/dev/null | yq -r '.[].sandbox_port'"

# Clear all forwards
assert_ok    "port-forward --clear removes device" locki port-forward -m "$LOGIN" --clear
sleep 3
assert_fail  "cleared forward is unreachable" bash -c "nc -4 -w2 127.0.0.1 $pf_host_port"

# Random host port with :sandbox_port syntax (different sandbox port)
random_host_port=$(locki port-forward -m "$LOGIN" --json :9222 2>/dev/null | json_field '[0].host_port' || true)
if [[ -n "$random_host_port" && "$random_host_port" -ge 1024 ]]; then
    pass ":port assigns random host port >= 1024"
else
    fail ":port assigns random host port >= 1024 (got '$random_host_port')"
fi
assert_ok    ":port forward cleaned up" locki port-forward -m "$LOGIN" --clear

# Reject privileged ports
assert_fail  "port < 1024 rejected" locki port-forward -m "$LOGIN" 80

# ── registry pull-through cache ──────────────────────────────────────────────

echo
echo "Testing registry pull-through cache..."

assert_output "docker is podman" "podman" locki x -m "$LOGIN" docker --version
assert_ok "podman shim works directly" locki x -m "$LOGIN" podman --version
assert_output "registry mirrors configured" "10.99.0.1:5001" locki x -m "$LOGIN" cat /etc/containers/registries.conf.d/99-locki-mirrors.conf
assert_ok "docker pull from docker.io" locki x -m "$LOGIN" docker pull -q alpine:3.20
# Unqualified, non-aliased short name must resolve to docker.io without a TTY prompt
assert_ok "docker pull with short name" locki x -m "$LOGIN" docker pull -q memcached:1.6-alpine
assert_ok "docker pull from ghcr.io" locki x -m "$LOGIN" docker pull -q ghcr.io/astral-sh/uv:latest
assert_ok "docker run works" locki x -m "$LOGIN" docker run --rm alpine:3.20 true
assert_ok "docker API socket responds" locki x -m "$LOGIN" curl -sf --unix-socket /run/docker.sock http://d/_ping
# Proxied blobs are committed to the cache asynchronously — allow a moment
sleep 5
assert_ok "docker.io pull populates registry cache" "$LIMACTL" shell --start --workdir=/ locki -- \
    sudo bash -c 'test -n "$(ls -A /var/cache/locki/registry-cache/docker)"'
assert_ok "ghcr.io pull populates registry cache" "$LIMACTL" shell --start --workdir=/ locki -- \
    sudo bash -c 'test -n "$(ls -A /var/cache/locki/registry-cache/ghcr)"'

# ── concurrent exec on a new sandbox ─────────────────────────────────────────

echo
echo "Testing concurrent exec on a new sandbox..."

RACE=$(new_sandbox_id)
locki x -m "$RACE" echo race-1 >"$TMPDIR_ROOT/race1.out" 2>/dev/null &
RACE_PID=$!
race2_out=$(locki x -m "$RACE" echo race-2 2>/dev/null) || true
wait "$RACE_PID" || true
if [[ "$(cat "$TMPDIR_ROOT/race1.out")" == *race-1* && "$race2_out" == *race-2* ]]; then
    pass "concurrent execs on fresh sandbox both succeed"
else
    fail "concurrent execs on fresh sandbox both succeed (race1: '$(cat "$TMPDIR_ROOT/race1.out")', race2: '$race2_out')"
fi

# ── locki new ──────────────────────────────────────────────────────────────

echo
echo "Testing locki new..."

NEW_OUT=$(locki new --json 2>/dev/null)
NEW_ID=$(printf '%s\n' "$NEW_OUT" | json_field id)
NEW_PATH=$(printf '%s\n' "$NEW_OUT" | json_field path)
assert_ok    "locki new --json prints sandbox id" test -n "$NEW_ID"
assert_ok    "locki new creates worktree dir" test -d "$NEW_PATH"
assert_output "worktree dir uses <repo>-locki-<id> format" "/r-locki-$NEW_ID" echo "$NEW_PATH"
assert_output "locki new --json prints matching branch" "untitled#locki-$NEW_ID" printf '%s\n' "$(printf '%s\n' "$NEW_OUT" | json_field branch)"
assert_ok    "locki new keeps stdout empty without --json" test -z "$(locki new 2>/dev/null)"

# ── sandbox creation with --new ─────────────────────────────────────────

echo
echo "Testing sandbox creation with --new..."

assert_output "--new creates sandbox" "create-ok" locki x --new echo create-ok
assert_fail "unknown substring rejects" locki x -m nonexistent-branch echo nope

# ── locki list outside git repo ─────────────────────────────────────────────

echo
echo "Testing locki list and outside-git-repo behavior..."

pushd /tmp >/dev/null
assert_ok    "locki list works outside git repo" locki list
assert_output "locki list sees sandboxes outside git repo" "$AUTH" locki list
assert_output "locki list --json includes sandbox id" "$AUTH" bash -c "locki list --json 2>/dev/null | yq -r '.[].id'"
assert_output "locki vm status --json reports running" "running" bash -c "locki vm status --json 2>/dev/null | yq -r '.vm'"
assert_ok    "locki x outside git repo with -m" locki x -m "$AUTH" echo 5
popd >/dev/null

# ── locki include ──────────────────────────────────────────────────────────

echo
echo "Testing locki include..."

REMOTE2="$TMPDIR_ROOT/my_other_repo.git"
REPO2="$TMPDIR_ROOT/r2"
git init --bare "$REMOTE2" >/dev/null
git clone "$REMOTE2" "$REPO2" >/dev/null 2>&1
git -C "$REPO2" config user.name "Locki Test"
git -C "$REPO2" config user.email "locki@example.com"
echo hello > "$REPO2/hello.txt"
git -C "$REPO2" add hello.txt
git -C "$REPO2" commit -m "initial repo2" >/dev/null
git -C "$REPO2" push >/dev/null 2>&1

INCLUDE_NAME="$(basename "$REPO2")"
INCLUDE_PATH="$WORKTREE_A/.locki/include/$INCLUDE_NAME"

INCLUDE_OUT=$(locki include -m "$AUTH" --repo "$REPO2" --json 2>/dev/null || true)
assert_output "locki include --json prints include path" "\"path\": \"$INCLUDE_PATH\"" printf '%s\n' "$INCLUDE_OUT"
assert_ok    "include folder exists"              test -d "$INCLUDE_PATH"
assert_ok    "include .git pointer exists"        test -f "$INCLUDE_PATH/.git"
assert_output "include branch named #locki-<id>"  "untitled#locki-$AUTH" git -C "$INCLUDE_PATH" branch --show-current

# Second include call for same repo should fail (collision).
assert_fail  "duplicate include rejected"         locki include -m "$AUTH" --repo "$REPO2"

# Git commands inside the include go through the command bridge.
assert_output "git status works inside include"   "nothing to commit" \
    locki x -m "$AUTH" bash -c "cd $INCLUDE_PATH && git status"

# Commit inside the include.
echo from-include | locki x -m "$AUTH" bash -c "cat > $INCLUDE_PATH/include-file.txt"
locki x -m "$AUTH" bash -c "cd $INCLUDE_PATH && git add --all && git commit --message='inside include'"
assert_output "include commit landed"             "inside include" git -C "$INCLUDE_PATH" log -1 --format=%s

# Tampering with the include's .git pointer is auto-repaired by command bridge.
ORIGINAL_DOTGIT=$(cat "$INCLUDE_PATH/.git")
echo "gitdir: /tmp/evil" > "$INCLUDE_PATH/.git"
assert_ok   "tampered .git is auto-repaired" locki x -m "$AUTH" bash -c "cd $INCLUDE_PATH && git status"
assert_output ".git restored from metadata" "$ORIGINAL_DOTGIT" cat "$INCLUDE_PATH/.git"

# ── branch verification on non-conforming worktree ──────────────────────────

echo
echo "Testing branch verification on non-conforming worktree..."

WORKTREE_B=$(git worktree list --porcelain | grep -B2 "branch refs/heads/untitled#locki-$LOGIN" | head -1 | sed 's/worktree //')
git -C "$WORKTREE_B" checkout -b rogue-branch 2>/dev/null
assert_output "worktree switched to rogue branch" "rogue-branch" git -C "$WORKTREE_B" branch --show-current
assert_output "locki x auto-fixes branch" "fix-ok" locki x -m "$LOGIN" echo fix-ok
assert_output "branch renamed with locki suffix" "rogue-branch#locki-$LOGIN" git -C "$WORKTREE_B" branch --show-current

# ── worktree cleanup ─────────────────────────────────────────────────────────

echo
echo "Testing worktree removal..."

if REMOVE_OUT=$(locki remove -m "$AUTH" --force --json 2>/dev/null); then pass "locki remove works"; else fail "locki remove works"; fi
assert_output "locki remove --json reports removed id" "\"id\": \"$AUTH\"" printf '%s\n' "$REMOVE_OUT"
assert_fail "removed worktree dir is gone" test -d "$WORKTREE"
assert_fail "included worktree dir is gone" test -d "$INCLUDE_PATH"
# repo2 should no longer list the worktree
assert_fail "include worktree removed from source repo" bash -c "git -C '$REPO2' worktree list | grep -q '$INCLUDE_PATH'"

# ── summary ──────────────────────────────────────────────────────────────────

echo
echo "════════════════════════════════════════"
echo "  $PASS passed, $FAIL failed"
echo "  cold start: ${cold_start}s / warm start: ${warm_start}s / hot start: ${hot_start}s"
if [[ $FAIL -gt 0 ]]; then
    echo -e "  failures:$ERRORS"
fi
echo "════════════════════════════════════════"

exit $FAIL
