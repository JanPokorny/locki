#!/bin/bash
# Locki hook wrapper — runs the original hook inside the sandbox for locki worktrees.
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_NAME="$(basename "$0")"
WRAPPED="$HOOK_DIR/$HOOK_NAME.locki-wrapped"
LOCKI_WORKTREES="$HOME/.locki/worktrees"

if [[ "$PWD" == "$LOCKI_WORKTREES"/* ]]; then
    branch=$(git rev-parse --abbrev-ref HEAD)
    hook_body=$(cat "$WRAPPED")
    exec locki shell "$branch" -c "$hook_body"
else
    exec "$WRAPPED" "$@"
fi
