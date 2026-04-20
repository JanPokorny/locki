from __future__ import annotations

import datetime
import importlib.resources
import os
import pathlib
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property

import click

from locki.paths import DENIED_LOG, WORKTREES, WORKTREES_META

State = tuple[int, frozenset[str]]  # (positional cursor, used flag keys)


# ── Compound parts ────────────────────────────────────────────────────────────


@dataclass
class Literal:
    """Literal text segment inside a compound token."""

    text: str


@dataclass
class Placeholder:
    """A `<name>` segment inside a compound token."""

    name: str


CompoundPart = Literal | Placeholder


# ── Context ───────────────────────────────────────────────────────────────────


class Context:
    """Per-invocation placeholder resolver. Subprocess lookups are cached."""

    def __init__(self, wt_id: str) -> None:
        self.wt_id = wt_id

    @cached_property
    def gh_repo(self) -> tuple[str, str]:
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
        return owner, name

    @cached_property
    def owned_stash_refs(self) -> list[str]:
        tag = f"#locki-{self.wt_id}"
        result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
        return [line.split(":", 1)[0] for line in result.stdout.splitlines() if tag in line]

    def compound(self, parts: list[CompoundPart]) -> re.Pattern[str]:
        """Build a `re.fullmatch`-ready pattern from compound parts."""
        buf: list[str] = []
        for part in parts:
            if isinstance(part, Literal):
                buf.append(re.escape(part.text))
            elif part.name == "wt-id":
                buf.append(re.escape(self.wt_id))
            elif part.name == "owner":
                buf.append(re.escape(self.gh_repo[0]))
            elif part.name == "repo":
                buf.append(re.escape(self.gh_repo[1]))
            elif part.name == "owned-stash-ref":
                refs = self.owned_stash_refs
                buf.append("(?:" + "|".join(re.escape(r) for r in refs) + ")" if refs else r"(?!)")
            elif part.name == "number":
                buf.append(r"\d+")
            else:
                buf.append(r".+?")
        return re.compile("".join(buf))


@dataclass
class MatchContext:
    positionals: list[str]
    flags: dict[str, str]
    ctx: Context


# ── AST ───────────────────────────────────────────────────────────────────────
# Each node yields all successful continuations of a match as (pos, used) pairs;
# backtracking falls out of `yield from`.


@dataclass
class Compound:
    """Positional: literal text interleaved with `<placeholder>`s."""

    parts: list[CompoundPart]

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        if pos < len(mc.positionals) and mc.ctx.compound(self.parts).fullmatch(mc.positionals[pos]):
            yield pos + 1, used


@dataclass
class BoolFlag:
    name: str

    @property
    def key(self) -> str:
        return self.name.replace("-", "_")

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        if self.key not in used and mc.flags.get(self.key) == "":
            yield pos, used | {self.key}


@dataclass
class ValueFlag:
    name: str
    parts: list[CompoundPart]

    @property
    def key(self) -> str:
        return self.name.replace("-", "_")

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        val = mc.flags.get(self.key)
        if self.key not in used and val is not None and mc.ctx.compound(self.parts).fullmatch(val):
            yield pos, used | {self.key}


@dataclass
class Separator:
    """Literal `--` in the grammar; no-op at match time (argv's `--` is pre-consumed)."""

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        yield pos, used


@dataclass
class Optional:
    inner: Node

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        yield pos, used
        yield from self.inner.match(pos, used, mc)


@dataclass
class Alternatives:
    alts: list[Node]

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        for alt in self.alts:
            yield from alt.match(pos, used, mc)


@dataclass
class Sequence:
    items: list[Node]

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        def go(i: int, p: int, u: frozenset[str]) -> Iterator[State]:
            if i == len(self.items):
                yield p, u
                return
            for p2, u2 in self.items[i].match(p, u, mc):
                yield from go(i + 1, p2, u2)

        yield from go(0, pos, used)


@dataclass
class Repetition:
    inner: Node

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[State]:
        def go(p: int, u: frozenset[str]) -> Iterator[State]:
            for p2, u2 in self.inner.match(p, u, mc):
                yield p2, u2
                yield from go(p2, u2)

        yield from go(pos, used)


Node = Compound | BoolFlag | ValueFlag | Separator | Optional | Alternatives | Sequence | Repetition


# ── Parser ────────────────────────────────────────────────────────────────────


class Parser:
    """Parse one grammar line into an AST node.

    Tokens:
      `...`                                  ellipsis (postfix "one or more"; whitespace-separated)
      `--flag`, `--flag=<compound>`, `--`    long flag (optionally with compound value) or bare separator
      `(`, `)`, `[`, `]`, `|`                 grouping metacharacters
      compound                                literal text interleaved with `<placeholder>`s
    """

    _COMPOUND_BODY = r"(?:<[^>]+>|[^<>\s()\[\]|])+"
    _TOKEN_RE = re.compile(rf"\.\.\.|--(?:[a-z][\w-]*(?:={_COMPOUND_BODY})?)?|[()|\[\]]|{_COMPOUND_BODY}")
    _COMPOUND_PART_RE = re.compile(r"<([^>]+)>|([^<>]+)")

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = self._TOKEN_RE.findall(text)
        self.idx = 0

    @classmethod
    def parse_line(cls, text: str) -> Node:
        parser = cls(text)
        tree = parser._alt()
        if parser.idx != len(parser.tokens):
            raise ValueError(f"Unparsed trailing tokens in: {text!r}")
        return tree

    def _peek(self) -> str | None:
        return self.tokens[self.idx] if self.idx < len(self.tokens) else None

    def _eat(self) -> str:
        tok = self.tokens[self.idx]
        self.idx += 1
        return tok

    def _expect(self, tok: str) -> None:
        if self._eat() != tok:
            raise ValueError(f"Expected {tok!r} in grammar line: {self.text!r}")

    def _alt(self) -> Node:
        seqs: list[Node] = [self._seq()]
        while self._peek() == "|":
            self._eat()
            seqs.append(self._seq())
        return Alternatives(seqs) if len(seqs) > 1 else seqs[0]

    def _seq(self) -> Node:
        items: list[Node] = []
        while self._peek() not in (None, ")", "]", "|"):
            items.append(self._item())
        return Sequence(items)

    def _item(self) -> Node:
        tok = self._eat()
        node: Node
        if tok == "[":
            node = Optional(self._alt())
            self._expect("]")
        elif tok == "(":
            node = self._alt()
            self._expect(")")
        elif tok == "--":
            node = Separator()
        elif tok.startswith("--") and "=" in tok:
            name, _, val_text = tok[2:].partition("=")
            node = ValueFlag(name, self._compound_parts(val_text))
        elif tok.startswith("--"):
            node = BoolFlag(tok[2:])
        else:
            node = Compound(self._compound_parts(tok))
        if self._peek() == "...":
            self._eat()
            node = Repetition(node)
        return node

    @classmethod
    def _compound_parts(cls, text: str) -> list[CompoundPart]:
        return [
            Placeholder(m.group(1)) if m.group(1) is not None else Literal(m.group(2))
            for m in cls._COMPOUND_PART_RE.finditer(text)
        ]


# ── Ruleset ───────────────────────────────────────────────────────────────────


class Ruleset:
    def __init__(self, rules: list[Node]) -> None:
        self.rules = rules

    @classmethod
    def from_markdown(cls, md: str) -> Ruleset:
        """Parse every non-blank line inside ```locki-self-service-command-filter fences as a grammar rule."""
        lines: list[str] = []
        in_block = False
        for raw in md.splitlines():
            line = raw.strip()
            if line == "```locki-self-service-command-filter":
                in_block = True
            elif in_block and line.startswith("```"):
                in_block = False
            elif in_block and line:
                lines.append(line)
        return cls([Parser.parse_line(line) for line in lines])

    def is_allowed(self, positionals: list[str], flags: dict[str, str], wt_id: str) -> bool:
        """`--help` is always allowed; every other flag must be consumed by the matching rule."""
        effective = {k: v for k, v in flags.items() if k != "help"}
        mc = MatchContext(positionals, effective, Context(wt_id))
        expected = set(effective)
        target = len(positionals)
        return any(
            p == target and used == expected for rule in self.rules for p, used in rule.match(0, frozenset(), mc)
        )


RULESET = Ruleset.from_markdown((importlib.resources.files("locki") / "data" / "AGENTS.md").read_text())


# ── CLI entry point ───────────────────────────────────────────────────────────


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

    if not argv:
        print("Empty command.", file=sys.stderr)
        raise SystemExit(1)

    # Split argv into positionals and long flags; short flags are rejected.
    positionals: list[str] = []
    flags: dict[str, str] = {}
    rest_positional = False
    for arg in argv[1:]:
        if rest_positional:
            positionals.append(arg)
        elif arg == "--":
            rest_positional = True
        elif arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            flags[key.replace("-", "_")] = value
        elif arg.startswith("-"):
            print(f"Short flags not allowed: {arg!r}", file=sys.stderr)
            raise SystemExit(1)
        else:
            positionals.append(arg)

    exe = pathlib.Path(argv[0]).name

    # chdir first so `gh repo view` and `git stash list` run inside the worktree.
    os.chdir(str(cwd))

    if not RULESET.is_allowed([exe, *positionals], flags, wt_id):
        try:
            DENIED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DENIED_LOG.open("a") as fh:
                ts = datetime.datetime.now().isoformat(timespec="seconds")
                fh.write(f"{ts}\t{wt_id}\t{shlex.join(argv)}\n")
        except OSError:
            pass
        print(f"Command not allowed: {' '.join(argv)!r}", file=sys.stderr)
        raise SystemExit(1)

    if exe == "locki":
        os.execvp(sys.executable, [sys.executable, "-m", "locki", *argv[1:]])
    else:
        os.execvp(exe, [exe, *argv[1:]])
