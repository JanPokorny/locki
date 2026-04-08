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
    local actual
    actual=$("$@" 2>/dev/null) || true
    if [[ "$actual" == *"$expected"* ]]; then pass "$desc"; else fail "$desc (expected '$expected', got '$actual')"; fi
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
# Kill any stale sshd on port 7890 from previous runs
lsof -ti :7890 | xargs kill 2>/dev/null || true
cleanup() { lsof -ti :7890 | xargs kill 2>/dev/null || true; rm -rf "$TMPDIR_ROOT"; }
trap cleanup EXIT

export HOME="$TMPDIR_ROOT/h"
mkdir -p "$HOME"

VENV="$TMPDIR_ROOT/v"
REPO="$TMPDIR_ROOT/r"
PROJECT_ROOT="$(cd "$(dirname "$0")/.."; pwd)"

echo "Setting up venv and installing locki..."
uv venv "$VENV" --python 3.14
export PATH="$VENV/bin:$PATH"
uv pip install --python "$VENV/bin/python" "$PROJECT_ROOT"

REMOTE="$TMPDIR_ROOT/remote.git"

echo "Creating test repo..."
git init --bare "$REMOTE"
git clone "$REMOTE" "$REPO"
git -C "$REPO" commit --allow-empty -m "initial"
git -C "$REPO" push

cd "$REPO"

# ── cold start + parallel VM creation ────────────────────────────────────────

echo
echo "Testing cold start + parallel VM creation..."

cold_start=$(timed locki shell a -c "echo 1") || true
echo "  cold start: ${cold_start}s"

# branch b in parallel with a (VM already exists, but tests lock waiting)
assert_output "locki shell b runs" "2" locki shell b -c "echo 2"

# ── cache persistence across invocations ─────────────────────────────────────

echo
echo "Testing cache persistence..."

assert_ok "write to cache" locki shell a -c "mkdir -p /var/cache/locki && echo 42 > /var/cache/locki/test"
assert_ok "cached file persists" locki shell a -c "test -f /var/cache/locki/test"

# ── hook execution in guest ──────────────────────────────────────────────────

echo
echo "Testing hook execution in guest..."

HOOKS_DIR="$REPO/.git/hooks"
mkdir -p "$HOOKS_DIR"
WORKTREE=$(git worktree list --porcelain | grep -B2 "branch refs/heads/a" | head -1 | sed 's/worktree //')

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

assert_ok    "git status works"              locki shell a -c "git status"
assert_ok    "git log works"                 locki shell a -c "git log --oneline"
assert_ok    "git diff works"                locki shell a -c "git diff"
assert_ok    "git show works"                locki shell a -c "git show"
assert_fail  "git checkout is blocked"       locki shell a -c "git checkout main"
assert_fail  "git reset is blocked"          locki shell a -c "git reset --hard"
assert_fail  "short flags are blocked"       locki shell a -c "git commit -m test"

# ── git commit from sandbox ─────────────────────────────────────────────────

echo
echo "Testing git commit from sandbox..."

WORKTREE_A=$(git worktree list --porcelain | grep -B2 "branch refs/heads/a" | head -1 | sed 's/worktree //')
locki shell a -c "echo test-content > $WORKTREE_A/commit-test.txt && git add --all && git commit --message='simple commit'"
assert_output "simple commit landed" "simple commit" git -C "$WORKTREE_A" log -1 --format=%s

# Multi-line commit message (newlines triggered $'...' quoting bug)
locki shell a -c "echo more > $WORKTREE_A/commit-test2.txt && git add --all && git commit --message='multi line

second paragraph'"
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

locki shell a -c "echo hook-msg-test > $WORKTREE_A/hook-msg-file.txt && git add --all && git commit --message='test hook message'"
assert_output "commit-msg hook appended trailer" "Signed-off-by: Test Bot" git -C "$WORKTREE_A" log -1 --format=%b
assert_output "original message preserved" "test hook message" git -C "$WORKTREE_A" log -1 --format=%s

rm -f "$HOOKS_DIR/commit-msg"

# ── remote branch tracking ──────────────────────────────────────────────────

echo
echo "Testing remote branch tracking..."

# Create a branch on the remote that doesn't exist locally
git clone "$REMOTE" "$TMPDIR_ROOT/pusher"
git -C "$TMPDIR_ROOT/pusher" checkout -b dependabot/pip/apps/some-long-server-name/pip-security-4d376cd218
git -C "$TMPDIR_ROOT/pusher" commit --allow-empty -m "remote commit"
git -C "$TMPDIR_ROOT/pusher" push -u origin dependabot/pip/apps/some-long-server-name/pip-security-4d376cd218

# Verify the branch doesn't exist locally yet
assert_fail "remote branch not local yet" git -C "$REPO" rev-parse --verify refs/heads/dependabot/pip/apps/some-long-server-name/pip-security-4d376cd218

# locki shell should fetch and create the branch from remote
assert_ok "locki fetches remote branch" locki shell dependabot/pip/apps/some-long-server-name/pip-security-4d376cd218 -c "echo ok"

# Check the branch was created from the remote (has the remote commit)
REMOTE_ONLY_WT=$(git -C "$REPO" worktree list --porcelain | grep -B2 "branch refs/heads/dependabot/pip/apps/some-long-server-name/pip-security-4d376cd218" | head -1 | sed 's/worktree //')
assert_output "branch has remote commit" "remote commit" git -C "$REMOTE_ONLY_WT" log --oneline -1 --format=%s

# ── warm start (new container, existing VM) ──────────────────────────────────

echo
echo "Testing warm start..."

warm_start=$(timed locki shell c -c "echo 3") || true
echo "  warm start: ${warm_start}s"

# ── hot start (existing container) ───────────────────────────────────────────

echo
echo "Testing hot start..."

hot_start=$(timed locki shell c -c "echo 4") || true
echo "  hot start: ${hot_start}s"

# ── container isolation ──────────────────────────────────────────────────────

echo
echo "Testing container isolation..."

assert_ok "write secret in branch a" locki shell a -c "echo secret > /tmp/a-only"
assert_fail "branch b can't see branch a's /tmp" locki shell b -c "test -f /tmp/a-only"

# ── custom image via locki.toml ──────────────────────────────────────────────

echo
echo "Testing locki.toml custom image..."

cat > "$REPO/locki.toml" << 'TOML'
[incus_image]
aarch64 = "images:ubuntu/24.04"
x86_64 = "images:ubuntu/24.04"
TOML

assert_output "custom image container runs ubuntu" "Ubuntu" locki shell d -c "cat /etc/os-release"
rm -f "$REPO/locki.toml"

# ── worktree cleanup ─────────────────────────────────────────────────────────

echo
echo "Testing worktree removal..."

assert_ok "locki remove works" locki remove --force a
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
