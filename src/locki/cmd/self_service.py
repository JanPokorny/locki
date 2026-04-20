import importlib.resources
import os
import pathlib
import re
import shlex
import subprocess
import sys

import click

from locki.paths import WORKTREES, WORKTREES_META


def _extract_grammar(md: str) -> list[str]:
    """Return non-blank lines from all `locki-self-service-command-filter` code fences in *md*."""
    rules: list[str] = []
    in_block = False
    for raw in md.splitlines():
        line = raw.strip()
        if line == "```locki-self-service-command-filter":
            in_block = True
        elif in_block and line.startswith("```"):
            in_block = False
        elif in_block and line:
            rules.append(line)
    return rules

# Tokenizer: each token is one of
#   `...`                                  ellipsis (postfix "one or more" operator; must be whitespace-separated)
#   `--flag`, `--flag=<compound>`, `--`    long flag (optionally with a compound value pattern) or bare separator
#   `(`, `)`, `[`, `]`, `|`                 grouping metacharacter
#   compound                                literal-text chunks interleaved with `<placeholder>`s
_COMPOUND_BODY = r"(?:<[^>]+>|[^<>\s()\[\]|])+"
_TOKEN_RE = re.compile(rf"\.\.\.|--(?:[a-z][\w-]*(?:={_COMPOUND_BODY})?)?|[()|\[\]]|{_COMPOUND_BODY}")
_COMPOUND_PART_RE = re.compile(r"<([^>]+)>|([^<>]+)")

# Regex fragments for context-free validated placeholders. Any unknown
# placeholder name falls back to `.+?` (non-empty, non-greedy).  Placeholders
# that depend on runtime context (`wt-id`, `owner`, `repo`, `owned-stash-ref`)
# are substituted in `_compound_pattern`.
PLACEHOLDER_RE: dict[str, str] = {"number": r"\d+"}


def _parse_compound(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    for m in _COMPOUND_PART_RE.finditer(text):
        ph, lit = m.group(1), m.group(2)
        parts.append(("ph", ph) if ph is not None else ("lit", lit))
    return parts


def _compound_pattern(parts: list[tuple[str, str]], ctx: dict) -> str:
    out: list[str] = []
    for kind, val in parts:
        if kind == "lit":
            out.append(re.escape(val))
        elif val == "wt-id":
            out.append(re.escape(ctx["wt_id"]))
        elif val == "owner":
            out.append(re.escape(_ctx_gh(ctx)[0]))
        elif val == "repo":
            out.append(re.escape(_ctx_gh(ctx)[1]))
        elif val == "owned-stash-ref":
            refs = _ctx_stash_refs(ctx)
            out.append("(?:" + "|".join(re.escape(r) for r in refs) + ")" if refs else r"(?!)")
        else:
            out.append(PLACEHOLDER_RE.get(val, r".+?"))
    return "".join(out)


def _parse(text: str):
    """Parse one grammar line into an AST.

    Node shapes:
      ("cmp", [(kind, val), ...])  positional token; parts are literal text or <placeholder>
      ("f", name)                   boolean long flag
      ("fv", name, [parts])         long flag with a compound value pattern
      ("sep",)                      literal `--` separator (no-op; argv's `--` is pre-consumed)
      ("opt", node)                 optional
      ("alt", [seq, ...])          alternatives
      ("seq", [item, ...])         sequence
      ("rep", node)                 one-or-more
    """
    tokens = _TOKEN_RE.findall(text)
    idx = [0]

    def peek():
        return tokens[idx[0]] if idx[0] < len(tokens) else None

    def eat():
        tok = tokens[idx[0]]
        idx[0] += 1
        return tok

    def p_alt():
        seqs = [p_seq()]
        while peek() == "|":
            eat()
            seqs.append(p_seq())
        return ("alt", seqs) if len(seqs) > 1 else seqs[0]

    def p_seq():
        items = []
        while peek() not in (None, ")", "]", "|"):
            items.append(p_item())
        return ("seq", items)

    def p_item():
        tok = eat()
        if tok == "[":
            node = ("opt", p_alt())
            if eat() != "]":
                raise ValueError(f"Expected ']' in grammar line: {text!r}")
        elif tok == "(":
            node = p_alt()
            if eat() != ")":
                raise ValueError(f"Expected ')' in grammar line: {text!r}")
        elif tok == "--":
            node = ("sep",)
        elif tok.startswith("--") and "=" in tok:
            name, _, val_text = tok[2:].partition("=")
            node = ("fv", name, _parse_compound(val_text))
        elif tok.startswith("--"):
            node = ("f", tok[2:])
        else:
            node = ("cmp", _parse_compound(tok))
        if peek() == "...":
            eat()
            node = ("rep", node)
        return node

    tree = p_alt()
    if idx[0] != len(tokens):
        raise ValueError(f"Unparsed trailing tokens in: {text!r}")
    return tree


def _match(node, positionals, pos, flags, used, ctx):
    """Yield (new_pos, new_used_flags) for each successful match."""
    kind = node[0]
    if kind == "cmp":
        if pos < len(positionals) and re.fullmatch(_compound_pattern(node[1], ctx), positionals[pos]):
            yield pos + 1, used
    elif kind == "f":
        key = node[1].replace("-", "_")
        if key not in used and flags.get(key) == "":
            yield pos, used | {key}
    elif kind == "fv":
        key = node[1].replace("-", "_")
        val = flags.get(key)
        if key not in used and val is not None and re.fullmatch(_compound_pattern(node[2], ctx), val):
            yield pos, used | {key}
    elif kind == "sep":
        yield pos, used
    elif kind == "opt":
        yield pos, used
        yield from _match(node[1], positionals, pos, flags, used, ctx)
    elif kind == "alt":
        for alt in node[1]:
            yield from _match(alt, positionals, pos, flags, used, ctx)
    elif kind == "seq":
        items = node[1]

        def _seq_go(i, p, u):
            if i == len(items):
                yield p, u
                return
            for p2, u2 in _match(items[i], positionals, p, flags, u, ctx):
                yield from _seq_go(i + 1, p2, u2)

        yield from _seq_go(0, pos, used)
    elif kind == "rep":
        inner = node[1]

        def _rep_go(p, u):
            for p2, u2 in _match(inner, positionals, p, flags, u, ctx):
                yield p2, u2
                yield from _rep_go(p2, u2)

        yield from _rep_go(pos, used)


RULES = [_parse(line) for line in _extract_grammar((importlib.resources.files("locki") / "data" / "AGENTS.md").read_text())]


def _ctx_gh(ctx: dict) -> tuple[str, str]:
    """Lazily resolve and cache the current gh repo as (owner, name)."""
    if "_gh" not in ctx:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("Could not determine current gh repo.", file=sys.stderr)
            raise SystemExit(1)
        owner, _, name = result.stdout.strip().partition("/")
        if not owner or not name:
            print(f"Invalid repo from gh: {result.stdout.strip()!r}.", file=sys.stderr)
            raise SystemExit(1)
        ctx["_gh"] = (owner, name)
    return ctx["_gh"]


def _ctx_stash_refs(ctx: dict) -> list[str]:
    """Lazily resolve and cache stash refs owned by the current worktree."""
    if "_stash_refs" not in ctx:
        tag = f"#locki-{ctx['wt_id']}"
        result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
        refs = [line.split(":", 1)[0] for line in result.stdout.splitlines() if tag in line]
        ctx["_stash_refs"] = refs
    return ctx["_stash_refs"]


def is_allowed(positionals: list[str], flags: dict[str, str], wt_id: str) -> bool:
    """Test whether (positionals, flags) is matched by any grammar rule. `--help` is always allowed."""
    effective = {k: v for k, v in flags.items() if k != "help"}
    ctx = {"wt_id": wt_id}
    expected_flags = set(effective)
    for rule in RULES:
        for p, used in _match(rule, positionals, 0, effective, set(), ctx):
            if p == len(positionals) and used == expected_flags:
                return True
    return False


def parse_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split args into positionals and long flags.

    Raises ValueError for short flags.
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    rest_positional = False
    for arg in args:
        if rest_positional:
            positionals.append(arg)
        elif arg == "--":
            rest_positional = True
        elif arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            flags[key.replace("-", "_")] = value
        elif arg.startswith("-"):
            raise ValueError(f"Short flags not allowed: {arg!r}")
        else:
            positionals.append(arg)
    return positionals, flags


@click.command(hidden=True)
def self_service_cmd():
    """SSH forced command: validate and execute an allowed self-service command."""
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not cmd:
        print("No command specified.", file=sys.stderr)
        raise SystemExit(1)

    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        print(f"Failed to parse command: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    if len(parts) < 2:
        print("Usage: <cwd> <exe> [args...]", file=sys.stderr)
        raise SystemExit(1)

    cwd_str, *argv = parts

    # Validate worktree
    cwd = pathlib.Path(cwd_str).resolve()
    if not cwd.is_relative_to(WORKTREES.resolve()):
        print(f"Not a locki worktree: {cwd_str!r}", file=sys.stderr)
        raise SystemExit(1)
    wt_root = WORKTREES / cwd.relative_to(WORKTREES).parts[0]
    wt_id = wt_root.name
    meta_git = WORKTREES_META / wt_id / ".git"
    dot_git = wt_root / ".git"
    if not wt_root.is_dir() or not meta_git.exists() or not dot_git.is_file():
        print(f"Invalid worktree: {cwd_str!r}", file=sys.stderr)
        raise SystemExit(1)
    if dot_git.read_text().strip() != meta_git.read_text().strip():
        print("Worktree .git mismatch — possible tampering.", file=sys.stderr)
        raise SystemExit(1)

    # Validate command against allowlist
    if not argv:
        print("Empty command.", file=sys.stderr)
        raise SystemExit(1)
    exe = pathlib.Path(argv[0]).name
    try:
        positionals, flags = parse_args(argv[1:])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from None

    os.chdir(str(cwd))

    if not is_allowed([exe, *positionals], flags, wt_id):
        print(f"Command not allowed: {' '.join(argv)!r}", file=sys.stderr)
        raise SystemExit(1)

    if exe == "locki":
        os.execvp(sys.executable, [sys.executable, "-m", "locki", *argv[1:]])
    else:
        os.execvp(exe, [exe, *argv[1:]])
