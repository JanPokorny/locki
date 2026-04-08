#!/bin/bash
cmd=$(basename "$0")
set -- "$(pwd)" "$cmd" "$@"
q=""
for arg in "$@"
    do q="${q:+$q }'${arg//\'/\'\\\'\'}'";
done
exec ssh -F /root/.ssh/locki-ssh-config locki-proxy -- "$q"
