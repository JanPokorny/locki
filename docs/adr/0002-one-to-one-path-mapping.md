# File transfers map paths 1:1

`locki file pull/push` copies by relative path only -- worktree-relative equals
host-repo-relative, no source/destination pairs (unlike `incus file`, which
needs them because paths differ). This holds even for `.locki/tmp/` artifacts,
which land as an untracked `.locki/tmp/` in the host repo by design; remapping
them elsewhere was rejected as a second mapping rule to remember. The one
exception: a push source outside the host repo has no 1:1 path, so it lands in
the worktree's `.locki/tmp/` exchange area under its basename.
