#!/bin/bash
set -euo pipefail
ORIGINAL_HOOK="$(git rev-parse --git-common-dir)/hooks/$(basename "$0")"
if [[ ! -x "$ORIGINAL_HOOK" ]]; then exit 0; fi
HOOK_TMP="/tmp/locki-hook-$$"

file_args=()
for arg in "$@"; do
    [[ -f "$arg" ]] && file_args+=("$arg")
done

if [[ ${#file_args[@]} -gt 0 ]]; then
    tar -cpf - -P "${file_args[@]}" | locki x sh -c 'tar -xpf - -P'
fi

set +e
locki x sh -c '
  printf %s "$1" | base64 -d > "$2"; chmod +x "$2"
  hook="$2"; shift 2
  "$hook" "$@"; rc=$?
  rm -f "$hook"
  exit $rc
' _ "$(base64 < "$ORIGINAL_HOOK")" "$HOOK_TMP" "$@"
rc=$?
set -e

if [[ ${#file_args[@]} -gt 0 ]]; then
    locki x sh -c 'tar -cpf - -P "$@"' _ "${file_args[@]}" | tar -xpf - -P
fi

exit "$rc"
