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
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

export HOME="$TMPDIR_ROOT/h"
mkdir -p "$HOME"

VENV="$TMPDIR_ROOT/v"
REPO="$TMPDIR_ROOT/r"
PROJECT_ROOT="$(cd "$(dirname "$0")/.."; pwd)"

echo "Setting up venv and installing locki..."
uv venv "$VENV" --python 3.14
export PATH="$VENV/bin:$PATH"
uv pip install --python "$VENV/bin/python" "$PROJECT_ROOT"

echo "Creating test repo..."
mkdir -p "$REPO"
git -C "$REPO" init
git -C "$REPO" commit --allow-empty -m "initial"

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

# ── guest git is blocked ─────────────────────────────────────────────────────

echo
echo "Testing guest restrictions..."

assert_fail "git is blocked in guest" locki shell a -c "git status"

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
arm64 = "images:ubuntu/24.04"
amd64 = "images:ubuntu/24.04"
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
