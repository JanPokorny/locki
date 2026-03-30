#!/bin/bash
set -euxo pipefail
ORIGINAL_HOOK="$(git rev-parse --git-common-dir)/hooks/$(basename "$0")"
if [[ ! -x "$ORIGINAL_HOOK" ]]; then exit 0; fi
exec locki shell "$(git rev-parse --abbrev-ref HEAD)" -c "$(cat "$ORIGINAL_HOOK")"
