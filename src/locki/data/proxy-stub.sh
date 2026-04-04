#!/bin/bash
cmd=$(basename "$0")
set -- "$(pwd)" "$cmd" "$@"
exec ssh -F /root/.ssh/locki-ssh-config locki-proxy -- "${@@Q}"
