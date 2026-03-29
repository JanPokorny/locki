#!/bin/sh
# Stub that replaces git and gh inside locki containers.
# Symlinked from /usr/local/bin/git and /usr/local/bin/gh.
cmd=$(basename "$0")
echo "$cmd: not available inside the locki sandbox." >&2
echo "Use the 'locki' MCP tool instead:" >&2
echo "  run_host_command(worktree_path=\"\$PWD\", exe=\"$cmd\", args=[...])" >&2
exit 1
