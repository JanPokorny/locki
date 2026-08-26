# Transfer commands are host-only

`locki file pull/push` and the `locki rm` rescue write into the host repo's
working tree, so they are deliberately excluded from the sandbox command bridge:
a sandbox-initiated pull would let an agent place arbitrary files outside its
worktree, piercing the security boundary. Agents that want files exported must
ask the user to run `locki file pull` on the host.
