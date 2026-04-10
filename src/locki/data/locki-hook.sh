#!/bin/bash
set -euo pipefail
ORIGINAL_HOOK="$(git rev-parse --git-common-dir)/hooks/$(basename "$0")"
if [[ ! -x "$ORIGINAL_HOOK" ]]; then exit 0; fi
HOOK_TMP="/tmp/locki-hook-$$"

# Copy the hook script into the container
locki x tee "$HOOK_TMP" < "$ORIGINAL_HOOK" >/dev/null
locki x chmod +x "$HOOK_TMP"

# Copy file arguments (e.g. COMMIT_EDITMSG) into the container
file_args=()
for arg in "$@"; do
    if [[ -f "$arg" ]]; then
        file_args+=("$arg")
        locki x mkdir -p "$(dirname "$arg")"
        locki x tee "$arg" < "$arg" >/dev/null
    fi
done

# Run the hook
set +e
locki x "$HOOK_TMP" "$@"
rc=$?
set -e
locki x rm -f "$HOOK_TMP"

# Copy modified file arguments back from the container
for arg in ${file_args+"${file_args[@]}"}; do
    locki x cat "$arg" > "$arg"
done

exit "$rc"
