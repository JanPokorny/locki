#!/bin/sh
cmd=$(basename "$0")
echo "$cmd: not available inside the locki sandbox." >&2
echo "Use the 'locki' MCP tool instead:" >&2
echo "  run_host_command(worktree_path=\"$(pwd)\", exe=\"$cmd\", args=[...])" >&2
exit 1