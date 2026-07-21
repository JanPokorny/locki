"""Command-bridge grammar engine: parses the allowed-command grammar out of
AGENTS.md and matches candidate argv lines against it.  Pure matching logic —
the SSH entry point that invokes it lives in `locki internal command-bridge`."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache, cached_property

from locki.services.worktree import branch_suffix

# ── Command bridge grammar engine ───────────────────────────────────────────────


@dataclass
class PlaceholderRule:
    """A `<name>` segment inside a compound token; appears only inside `ArgRule.value`."""

    name: str


@dataclass
class ArgRule:
    """A positional (or flag value): literal strings interleaved with placeholders."""

    value: list[str | PlaceholderRule]

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[tuple[int, frozenset[str]]]:
        if pos < len(mc.positionals) and mc.ctx.compound(self.value).fullmatch(mc.positionals[pos]):
            yield pos + 1, used

    def walk_flags(self) -> Iterator[FlagRule]:
        yield from ()


@dataclass
class FlagRule:
    short_name: str | None  # Full form, e.g. "-s".
    long_name: str  # Full form, e.g. "--short".
    value: ArgRule | None  # None for bool flags.

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[tuple[int, frozenset[str]]]:
        key: str | None = None
        if self.long_name in mc.flags and self.long_name not in used:
            key = self.long_name
        elif self.short_name is not None and self.short_name in mc.flags and self.short_name not in used:
            key = self.short_name
        if key is None:
            return
        val = mc.flags[key]
        if self.value is None:
            if val == "":
                yield pos, used | {key}
        elif mc.ctx.compound(self.value.value).fullmatch(val):
            yield pos, used | {key}

    def walk_flags(self) -> Iterator[FlagRule]:
        yield self


@dataclass
class AlternativeRule:
    """`(a | b | c)` when `optional=False`, `[a | b | c]` when `optional=True`."""

    alternatives: list[Rule]
    optional: bool

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[tuple[int, frozenset[str]]]:
        if self.optional:
            yield pos, used
        for alt in self.alternatives:
            yield from alt.match(pos, used, mc)

    def walk_flags(self) -> Iterator[FlagRule]:
        for alt in self.alternatives:
            yield from alt.walk_flags()


@dataclass
class SequenceRule:
    """Ordered items; `last_repeats=True` means the last one matches one-or-more times."""

    sequence: list[Rule]
    last_repeats: bool

    def __post_init__(self) -> None:
        if self.last_repeats and not self.sequence:
            raise ValueError("SequenceRule with last_repeats=True must be non-empty")

    def match(self, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[tuple[int, frozenset[str]]]:
        yield from self._match_from(0, pos, used, mc)

    def _match_from(
        self, i: int, pos: int, used: frozenset[str], mc: MatchContext
    ) -> Iterator[tuple[int, frozenset[str]]]:
        if i >= len(self.sequence):
            yield pos, used
            return
        is_last = i == len(self.sequence) - 1
        for p2, u2 in self.sequence[i].match(pos, used, mc):
            if is_last:
                yield p2, u2
                if self.last_repeats:
                    yield from self._match_from(i, p2, u2, mc)
            else:
                yield from self._match_from(i + 1, p2, u2, mc)

    def walk_flags(self) -> Iterator[FlagRule]:
        for item in self.sequence:
            yield from item.walk_flags()


Rule = ArgRule | FlagRule | AlternativeRule | SequenceRule


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
            sys.exit("Could not determine current gh repo.")
        owner, _, name = result.stdout.strip().partition("/")
        if not owner or not name:
            sys.exit(f"Invalid repo from gh: {result.stdout.strip()!r}.")
        return owner, name

    @cached_property
    def owned_stash_refs(self) -> list[str]:
        tag = branch_suffix(self.wt_id)
        result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
        return [line.split(":", 1)[0] for line in result.stdout.splitlines() if tag in line]

    @cached_property
    def remotes(self) -> list[str]:
        result = subprocess.run(["git", "remote"], capture_output=True, text=True)
        return result.stdout.split()

    def compound(self, parts: list[str | PlaceholderRule]) -> re.Pattern[str]:
        """Build a `re.fullmatch`-ready pattern from literal strings and placeholders."""
        buf: list[str] = []
        for part in parts:
            match part:
                case str():
                    buf.append(re.escape(part))
                case PlaceholderRule("wt-id"):
                    buf.append(re.escape(self.wt_id))
                case PlaceholderRule("owner"):
                    buf.append(re.escape(self.gh_repo[0]))
                case PlaceholderRule("repo"):
                    buf.append(re.escape(self.gh_repo[1]))
                case PlaceholderRule("owned-stash-ref"):
                    refs = self.owned_stash_refs
                    buf.append("(?:" + "|".join(re.escape(r) for r in refs) + ")" if refs else r"(?!)")
                case PlaceholderRule("remote"):
                    remotes = self.remotes
                    buf.append("(?:" + "|".join(re.escape(r) for r in remotes) + ")" if remotes else r"(?!)")
                case PlaceholderRule("number"):
                    buf.append(r"\d+")
                case _:
                    buf.append(r".+?")
        return re.compile("".join(buf), re.DOTALL)


@dataclass
class MatchContext:
    positionals: list[str]
    flags: dict[str, str]
    ctx: Context


# ── Grammar ──────────────────────────────────────────────────────────────────
#
# A grammar line is parsed with Lark straight into the `Rule` AST.  A `seq` rule
# collects atoms (optionally followed by `...`) into a `SequenceRule`; an `alt`
# rule wraps multiple seqs in an `AlternativeRule`; `[...]` sets `optional=True`.


_COMPOUND_PART_RE = re.compile(r"<([^>]+)>|([^<>]+)")


def _compound_parts(text: str) -> list[str | PlaceholderRule]:
    return [
        PlaceholderRule(m.group(1)) if m.group(1) is not None else m.group(2) for m in _COMPOUND_PART_RE.finditer(text)
    ]


_GRAMMAR = r"""
alt: seq ("|" seq)*
seq: atom* ELLIPSIS?
?atom: group | opt | flag | compound
group: "(" alt ")"
opt:   "[" alt "]"
flag: FLAG
compound: COMPOUND

ELLIPSIS: "..."
FLAG.2:   /(?:-[a-zA-Z]\/)?--[a-z][\w-]*(?:=(?:<[^>]+>|[^<>\s()\[\]|])+)?|-[a-zA-Z](?:=(?:<[^>]+>|[^<>\s()\[\]|])+)?/
COMPOUND: /(?:<[^>]+>|[^<>\s()\[\]|])+/

%ignore /\s+/
"""


@cache
def _parser():
    """Build the grammar parser on first use — importing lark and constructing the
    LALR tables is too costly to do at module import time."""
    from lark import Lark, Token, Transformer

    class _ASTBuilder(Transformer):
        def alt(self, c: list[Rule]) -> Rule:
            return c[0] if len(c) == 1 else AlternativeRule(alternatives=list(c), optional=False)

        def seq(self, c: list[Rule | Token]) -> SequenceRule:
            last_repeats = bool(c) and isinstance(c[-1], Token) and c[-1].type == "ELLIPSIS"
            items = [x for x in c if not isinstance(x, Token)]
            return SequenceRule(sequence=items, last_repeats=last_repeats)  # pyrefly: ignore

        def group(self, c: list[Rule]) -> Rule:
            return c[0]

        def opt(self, c: list[Rule]) -> AlternativeRule:
            inner = c[0]
            if isinstance(inner, AlternativeRule) and not inner.optional:
                return AlternativeRule(alternatives=inner.alternatives, optional=True)
            return AlternativeRule(alternatives=[inner], optional=True)

        def flag(self, c: list[Token]) -> FlagRule:
            tok = str(c[0])
            short: str | None = None
            if tok.startswith("-") and not tok.startswith("--"):
                if tok[2:3] == "/":  # paired short+long, e.g. "-s/--short"
                    short = tok[:2]
                    tok = tok[3:]
                else:  # short-only flag, e.g. "-r" or "-L=<range>"
                    name, sep, value_text = tok.partition("=")
                    value = ArgRule(value=_compound_parts(value_text)) if sep == "=" else None
                    return FlagRule(short_name=name, long_name=name, value=value)
            name, sep, value_text = tok.partition("=")
            value = ArgRule(value=_compound_parts(value_text)) if sep == "=" else None
            return FlagRule(short_name=short, long_name=name, value=value)

        def compound(self, c: list[Token]) -> ArgRule:
            return ArgRule(value=_compound_parts(str(c[0])))

    return Lark(_GRAMMAR, start="alt", parser="lalr", transformer=_ASTBuilder())


# ── Ruleset ──────────────────────────────────────────────────────────────────


def _extract_prefix(rule: Rule) -> tuple[str, ...]:
    """Get the leading literal words (1 or 2) from a top-level SequenceRule."""
    if not isinstance(rule, SequenceRule):
        raise ValueError("Top-level rule must be a sequence")
    words: list[str] = []
    for item in rule.sequence:
        if isinstance(item, ArgRule) and len(item.value) == 1 and isinstance(item.value[0], str):
            words.append(item.value[0])
            if len(words) == 2:
                return tuple(words)
        else:
            break
    if words:
        return tuple(words)
    raise ValueError(f"Rule must start with at least one literal word, got {words}")


def _collect_value_flag_keys(rules: list[Rule]) -> frozenset[str]:
    keys: set[str] = set()
    for rule in rules:
        for flag in rule.walk_flags():
            if flag.value is not None:
                keys.add(flag.long_name)
                if flag.short_name is not None:
                    keys.add(flag.short_name)
    return frozenset(keys)


def _split_argv(args: list[str], value_flag_keys: frozenset[str]) -> tuple[list[str], dict[str, str]]:
    """Split args into positionals and flags keyed by their original form.

    Flags are stored with their full name (e.g. ``--limit``, ``-L``).
    For value-flags, ``--flag value``, ``--flag=value``, ``-x value``,
    ``-xvalue`` and ``-x=value`` all work; bool flags are standalone.
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    rest_positional = False
    i = 0
    while i < len(args):
        arg = args[i]
        if rest_positional:
            positionals.append(arg)
        elif arg == "--":
            rest_positional = True
        elif arg.startswith("--"):
            key, sep, value = arg.partition("=")
            if sep == "" and key in value_flag_keys and i + 1 < len(args) and not args[i + 1].startswith("-"):
                value = args[i + 1]
                i += 1
            flags[key] = value
        elif len(arg) >= 2 and arg[0] == "-":
            if arg[1:].isdigit():
                flags["--max-count"] = arg[1:]
                i += 1
                continue
            key = arg[:2]
            glued = arg[2:].removeprefix("=")
            if glued and key in value_flag_keys:
                flags[key] = glued
            elif glued:
                flags[key] = ""
                for ch in glued[:-1]:
                    flags[f"-{ch}"] = ""
                last = f"-{glued[-1]}"
                if last in value_flag_keys and i + 1 < len(args) and not args[i + 1].startswith("-"):
                    flags[last] = args[i + 1]
                    i += 1
                else:
                    flags[last] = ""
            elif key in value_flag_keys and i + 1 < len(args) and not args[i + 1].startswith("-"):
                flags[key] = args[i + 1]
                i += 1
            else:
                flags[key] = ""
        else:
            positionals.append(arg)
        i += 1
    return positionals, flags


@dataclass
class _RuleGroup:
    rules: list[Rule]
    lines: list[str]
    value_flag_keys: frozenset[str]


_GIT_TRANSPARENT_BOOL = {"--no-optional-locks", "--no-pager"}
# None = any value allowed
_GIT_TRANSPARENT_CONFIG: dict[str, set[str] | None] = {
    "log.showsignature": None,
    "core.quotepath": None,
    "protocol.file.allow": {"user", "never", "always"},
    "core.fsmonitor": {""},
    "core.pager": {""},
    "core.hookspath": {"/dev/null"},
}


def _strip_git_transparent(argv: list[str]) -> list[str]:
    """Strip harmless top-level git flags (before the subcommand) so they don't block matching."""
    if not argv or argv[0] != "git":
        return argv
    out = [argv[0]]
    i = 1
    while i < len(argv):
        if argv[i] in _GIT_TRANSPARENT_BOOL:
            i += 1
        elif argv[i] == "-c" and i + 1 < len(argv):
            key, _, value = argv[i + 1].partition("=")
            allowed = _GIT_TRANSPARENT_CONFIG.get(key.lower(), ...)
            if allowed is ... or (allowed is not None and value not in allowed):
                break
            i += 2
        elif argv[i] == "-C" and i + 1 < len(argv) and argv[i + 1] == ".":
            i += 2
        else:
            break
    out.extend(argv[i:])
    return out


class Ruleset:
    def __init__(self, groups: dict[tuple[str, ...], _RuleGroup]) -> None:
        self._groups = groups

    @classmethod
    def from_markdown(cls, md: str) -> Ruleset:
        """Parse every non-blank line inside ```locki-bridged-command-filter fences."""
        raw: dict[tuple[str, ...], tuple[list[Rule], list[str]]] = {}
        in_block = False
        for line_raw in md.splitlines():
            line = line_raw.strip()
            if line == "```locki-bridged-command-filter":
                in_block = True
            elif in_block and line.startswith("```"):
                in_block = False
            elif in_block and line:
                rule: Rule = _parser().parse(line)  # pyrefly: ignore
                prefix = _extract_prefix(rule)
                entry = raw.setdefault(prefix, ([], []))
                entry[0].append(rule)
                entry[1].append(line)
        return cls(
            {
                prefix: _RuleGroup(
                    rules=rules,
                    lines=lines,
                    value_flag_keys=_collect_value_flag_keys(rules),
                )
                for prefix, (rules, lines) in raw.items()
            }
        )

    def check(self, argv: list[str], wt_id: str) -> str | None:
        """Return None if allowed, or an error message."""
        if not argv:
            return f"Command not allowed: {shlex.join(argv)!r}"

        argv = _strip_git_transparent(argv)

        prefix: tuple[str, ...] | None = None
        group: _RuleGroup | None = None

        if len(argv) >= 2 and not argv[1].startswith("-"):
            prefix = (argv[0], argv[1])
            group = self._groups.get(prefix)

        if group is None:
            prefix = (argv[0],)
            group = self._groups.get(prefix)

        if group is None or prefix is None:
            return f"Command not allowed: {shlex.join(argv)!r}"

        positionals, flags = _split_argv(argv[len(prefix) :], group.value_flag_keys)
        effective = {k: v for k, v in flags.items() if k != "--help"}
        all_positionals = [*prefix, *positionals]
        mc = MatchContext(all_positionals, effective, Context(wt_id))
        expected = set(effective)
        target = len(all_positionals)

        if any(p == target and used == expected for rule in group.rules for p, used in rule.match(0, frozenset(), mc)):
            return None

        lines = "\n".join(f"  {line}" for line in group.lines)
        return f'Allowed forms of "{" ".join(prefix)}" are:\n{lines}'
