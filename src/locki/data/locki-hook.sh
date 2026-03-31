#!/bin/bash
set -euxo pipefail
ORIGINAL_HOOK="$(git rev-parse --git-common-dir)/hooks/$(basename "$0")"
if [[ ! -x "$ORIGINAL_HOOK" ]]; then exit 0; fi
HOOK_TMP="/tmp/locki-hook-$$"
locki shell -c "cat > $HOOK_TMP && chmod +x $HOOK_TMP" < "$ORIGINAL_HOOK"
exec locki shell -c "$HOOK_TMP \"\$@\"; rc=\$?; rm -f $HOOK_TMP; exit \$rc" -- "$@"
