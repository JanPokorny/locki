#!/bin/bash
# Locki per-worktree hook — runs the corresponding .git/hooks/<name> inside the sandbox.
HOOK_NAME="$(basename "$0")"
GIT_COMMON_DIR="$(git rev-parse --git-common-dir)"
ORIGINAL_HOOK="$GIT_COMMON_DIR/hooks/$HOOK_NAME"

if [[ ! -x "$ORIGINAL_HOOK" ]]; then
    exit 0
fi

branch=$(git rev-parse --abbrev-ref HEAD)
hook_body=$(cat "$ORIGINAL_HOOK")
exec locki shell "$branch" -c "$hook_body"
