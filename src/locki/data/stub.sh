#!/bin/sh
cmd=$(basename "$0")
echo "$cmd: not available inside the locki sandbox." >&2
exit 1
