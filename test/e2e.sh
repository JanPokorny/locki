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
kill_locki_sshd() { local pf="$XDG_RUNTIME_DIR/locki/sshd.pid"; [ -f "$pf" ] && kill "$(cat "$pf")" 2>/dev/null || true; }
kill_locki_sshd
cleanup() { kill_locki_sshd; rm -rf "$TMPDIR_ROOT"; }
trap cleanup EXIT

VENV="$TMPDIR_ROOT/v"
REPO="$TMPDIR_ROOT/r"
PROJECT_ROOT="$(cd "$(dirname "$0")/.."; pwd)"

echo "Setting up venv and installing locki..."
uv venv "$VENV" --python 3.14
export PATH="$VENV/bin:$PATH"
uv pip install --python "$VENV/bin/python" "$PROJECT_ROOT"

REMOTE="$TMPDIR_ROOT/my_repo.v2-test"

echo "Creating test repo..."
git init --bare "$REMOTE"
git clone "$REMOTE" "$REPO"
git -C "$REPO" commit --allow-empty -m "initial"
git -C "$REPO" push

cd "$REPO"

# ── cold start + parallel VM creation ────────────────────────────────────────

echo
echo "Testing cold start + parallel VM creation..."

cold_start=$(timed locki x --create -b auth echo 1) || true
echo "  cold start: ${cold_start}s"

# branch b in parallel with a (VM already exists, but tests lock waiting)
assert_output "locki x b runs" "2" locki x --create -b login echo 2

# ── cache persistence across invocations ─────────────────────────────────────

echo
echo "Testing cache persistence..."

locki x -b auth mkdir -p /var/cache/locki
assert_ok "write to cache" bash -c "echo 42 | locki x -b auth tee /var/cache/locki/test >/dev/null"
assert_ok "cached file persists" locki x -b auth test -f /var/cache/locki/test

# ── hook execution in guest ──────────────────────────────────────────────────

echo
echo "Testing hook execution in guest..."

HOOKS_DIR="$REPO/.git/hooks"
mkdir -p "$HOOKS_DIR"
WORKTREE=$(git worktree list --porcelain | grep -B2 "branch refs/heads/auth#locki-" | head -1 | sed 's/worktree //')

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

assert_ok    "git status works"              locki x -b auth git status
assert_ok    "git log works"                 locki x -b auth git log --oneline
assert_ok    "git diff works"                locki x -b auth git diff
assert_ok    "git show works"                locki x -b auth git show
assert_fail  "git checkout is blocked"       locki x -b auth git checkout main
assert_fail  "git reset --hard (no ref) is blocked" locki x -b auth git reset --hard
assert_ok    "git reset <ref> --hard works"  locki x -b auth git reset HEAD --hard
assert_fail  "short flags are blocked"       locki x -b auth git commit -m test

# ── git commit from sandbox ─────────────────────────────────────────────────

echo
echo "Testing git commit from sandbox..."

WORKTREE_A=$(git worktree list --porcelain | grep -B2 "branch refs/heads/auth#locki-" | head -1 | sed 's/worktree //')
echo test-content | locki x -b auth tee "$WORKTREE_A/commit-test.txt" >/dev/null
locki x -b auth git add --all
locki x -b auth git commit --message='simple commit'
assert_output "simple commit landed" "simple commit" git -C "$WORKTREE_A" log -1 --format=%s

# Multi-line commit message (newlines triggered $'...' quoting bug)
echo more | locki x -b auth tee "$WORKTREE_A/commit-test2.txt" >/dev/null
locki x -b auth git add --all
locki x -b auth git commit --message='multi line

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

echo hook-msg-test | locki x -b auth tee "$WORKTREE_A/hook-msg-file.txt" >/dev/null
locki x -b auth git add --all
locki x -b auth git commit --message='test hook message'
assert_output "commit-msg hook appended trailer" "Signed-off-by: Test Bot" git -C "$WORKTREE_A" log -1 --format=%b
assert_output "original message preserved" "test hook message" git -C "$WORKTREE_A" log -1 --format=%s

rm -f "$HOOKS_DIR/commit-msg"

# ── warm start (new container, existing VM) ──────────────────────────────────

echo
echo "Testing warm start..."

warm_start=$(timed locki x --create -b release echo 3) || true
echo "  warm start: ${warm_start}s"

# ── hot start (existing container) ───────────────────────────────────────────

echo
echo "Testing hot start..."

hot_start=$(timed locki x -b release echo 4) || true
echo "  hot start: ${hot_start}s"

# ── container isolation ──────────────────────────────────────────────────────

echo
echo "Testing container isolation..."

assert_ok "write secret in sandbox a" bash -c "echo secret | locki x -b auth tee /tmp/a-only >/dev/null"
assert_fail "sandbox b can't see sandbox a's /tmp" locki x -b login test -f /tmp/a-only

# ── custom image via locki.toml ──────────────────────────────────────────────

echo
echo "Testing locki.toml custom image..."

cat > "$REPO/locki.toml" << 'TOML'
[incus_image]
aarch64 = "images:ubuntu/24.04"
x86_64 = "images:ubuntu/24.04"
TOML

assert_output "custom image container runs ubuntu" "Ubuntu" locki x --create -b custom-img cat /etc/os-release
rm -f "$REPO/locki.toml"

# ── port forwarding ─────────────────────────────────────────────────────────

echo
echo "Testing port forwarding..."

# Install ncat in the container (base image doesn't include it)
locki x -b login dnf install -y nmap-ncat

# Start a persistent listener inside the container
locki x -b login bash -c "nohup bash -c 'while true; do echo pf-ok | ncat -l 9111; done' &>/dev/null &"

# Forward host 9111 -> container 9111
assert_ok    "port-forward adds device" locki port-forward -b login 9111

# Wait for Lima to detect and forward the new listening port
pf_ok=false
for i in $(seq 1 10); do
    if result=$(nc -4 -w2 127.0.0.1 9111 2>/dev/null) && [[ "$result" == *"pf-ok"* ]]; then
        pf_ok=true; break
    fi
    sleep 1
done
if $pf_ok; then pass "port-forward is reachable"; else fail "port-forward is reachable (timed out after 10s)"; fi

# Clear all forwards
assert_ok    "port-forward --clear removes device" locki port-forward -b login --clear
sleep 3
assert_fail  "cleared forward is unreachable" bash -c "nc -4 -w2 127.0.0.1 9111"

# Random host port with :container_port syntax
random_output=$(locki port-forward -b login :9222 2>/dev/null) || true
random_host_port=$(echo "$random_output" | grep -oE '^[0-9]+')
if [[ "$random_host_port" -ge 1024 ]]; then pass ":port assigns random host port >= 1024"; else fail ":port assigns random host port >= 1024 (got '$random_host_port')"; fi
assert_output ":port output shows container port" ":9222" echo "$random_output"
assert_ok    ":port forward cleaned up" locki port-forward -b login --clear

# Reject privileged ports
assert_fail  "port < 1024 rejected" locki port-forward -b login 80

# ── sandbox creation with --new ─────────────────────────────────────────────

echo
echo "Testing sandbox creation with --create..."

assert_output "--create creates sandbox" "create-ok" locki x --create -b test-create echo create-ok
assert_fail "unknown substring rejects" locki x -b nonexistent-branch echo nope

# ── worktree cleanup ─────────────────────────────────────────────────────────

echo
echo "Testing worktree removal..."

assert_ok "locki remove works" locki remove -b auth --force
assert_fail "removed worktree dir is gone" test -d "$WORKTREE"

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
