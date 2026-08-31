# Last used is a stamp, not mtime

A sandbox's "last used" time is an explicit stamp in its metadata dir, written when
the sandbox is opened (`ai`/`x`/`cd`/`ide`) and refreshed by the daemon while the
container has live Incus operations. Walking worktree file mtimes was rejected: it
would catch more activity (an IDE session left open re-stamps nothing after opening
— an accepted blind spot), but costs a directory walk per sandbox on every
`locki ls`, while the stamp is one file read and needs no VM. Sandboxes that predate
stamping fall back to the metadata dir's mtime rather than showing blank.
