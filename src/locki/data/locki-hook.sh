#!/bin/bash
set -euo pipefail
ORIGINAL_HOOK="$(git rev-parse --git-common-dir)/hooks/$(basename "$0")"
if [[ ! -x "$ORIGINAL_HOOK" ]]; then exit 0; fi
HOOK_TMP="/tmp/locki-hook-$$"
sq() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

# Copy the hook script into the container
locki shell -c "cat > $(sq "$HOOK_TMP") && chmod +x $(sq "$HOOK_TMP")" < "$ORIGINAL_HOOK"

# Copy file arguments (e.g. COMMIT_EDITMSG) into the container
file_args=()
for arg in "$@"; do
    if [[ -f "$arg" ]]; then
        file_args+=("$arg")
        locki shell -c "mkdir -p $(sq "$(dirname "$arg")") && cat > $(sq "$arg")" < "$arg"
    fi
done

# Run the hook
q="$(sq "$HOOK_TMP")"
for arg in "$@"; do q="$q $(sq "$arg")"; done
set +e
locki shell -c "$q; rc=\$?; rm -f $(sq "$HOOK_TMP"); exit \$rc"
rc=$?
set -e

# Copy modified file arguments back from the container
for arg in ${file_args+"${file_args[@]}"}; do
    locki shell -c "cat $(sq "$arg")" > "$arg"
done

exit "$rc"
