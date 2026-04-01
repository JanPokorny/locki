#!/bin/bash
set -euxo pipefail
ORIGINAL_HOOK="$(git rev-parse --git-common-dir)/hooks/$(basename "$0")"
if [[ ! -x "$ORIGINAL_HOOK" ]]; then exit 0; fi

BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
GUEST_HOOK_TMP="/tmp/locki-hook-$$"
HOOK_B64="$(base64 < "$ORIGINAL_HOOK" | tr -d '\n')"

locki_shell() {
  if [[ -n "$BRANCH" ]]; then
    locki shell "$BRANCH" "$@"
  else
    locki shell "$@"
  fi
}

locki_shell -c 'printf %s "$1" | base64 -d > "$2" && chmod +x "$2"' -- "$HOOK_B64" "$GUEST_HOOK_TMP"
locki_shell -c "\"$GUEST_HOOK_TMP\" \"\$@\"; rc=\$?; rm -f \"$GUEST_HOOK_TMP\"; exit \$rc" -- "$@"
