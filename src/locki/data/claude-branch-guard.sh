#!/bin/sh
# Claude Code PreToolUse hook: deny all tool use while the branch is still
# untitled#locki-<wt-id>, except git branch/switch commands (the rename itself).
# Fails open on any anomaly. Marker caches the named state per container so the
# bridged git call does not run on every tool use.
[ -e /tmp/.locki-branch-named ] && exit 0
input=$(cat)
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
case "$PWD" in "${LOCKI_WORKTREES_HOME:-//}"/*) ;; *) exit 0 ;; esac
branch=$(git branch --show-current 2>/dev/null)
case "$branch" in
untitled\#locki-*)
    printf '%s' "$input" | grep -qE 'git (branch|switch|checkout)' && exit 0
    echo "Start by renaming the current untitled branch to something descriptive, then retry the tool call: git branch <new-name>${branch#untitled} --move" >&2
    exit 2
    ;;
"") ;; # detached HEAD or bridge hiccup: fail open, re-check next time
*) touch /tmp/.locki-branch-named ;;
esac
exit 0
