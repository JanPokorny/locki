#!/bin/sh
cmd=$(basename "$0")
sq() { printf "'"; printf '%s' "$1" | sed "s/'/'\\\\''/g"; printf "' "; }
args="$(sq "$(pwd)")$(sq "$cmd")"
for a in "$@"; do args="$args$(sq "$a")"; done
exec ssh -F /root/.ssh/locki-ssh-config locki-proxy -- "$args"
