# --raw copies gitignored files wholesale

`--raw` copies every gitignored file (except `.git` and `.locki`) -- no
skip-list for heavyweight dirs (node_modules, .venv), which would be wrong for
someone and need maintaining. The cost is deliberate: gitignored files often
hold credentials, and a token copied into the sandbox is usable by the agent
directly (e.g. against the GitHub API), beyond the bridge's branch scoping --
so the copy warns when ignored files travel.
