"""Internal commands invoked by Locki itself — not for direct end-user use.

* `locki internal cleanup` — one-shot: stop idle containers, remove orphans, power off idle VM.
* `locki internal daemon`  — long-running host daemon: asyncssh forced-command proxy + cleanup scheduler.
* `locki internal command-bridge` — SSH forced bridged command handler: validate and run a whitelisted command.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache, cached_property

import asyncssh
import click
from lark import Lark, Token, Transformer

from locki.paths import DATA, DENIED_LOG, PACKAGE_DATA, PID_FILE, PORT_FILE, RUNTIME, STATE, WORKTREES, WORKTREES_META
from locki.utils import limactl, vm_status

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 600
VM_IDLE_TIMEOUT = 600
CLEANUP_INTERVAL = 60

LAST_ACTIVE_FILE = STATE / "cleanup" / "last-active.json"
VM_IDLE_SINCE_FILE = STATE / "cleanup" / "vm-idle-since"
HOST_KEY = STATE / "ssh" / "host_key"
CLIENT_KEY = DATA / "home" / ".ssh" / "id_locki"
AUTHORIZED_KEYS_FILE = STATE / "ssh" / "authorized_keys"


def _incus(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [limactl(), "shell", "--tty=false", "locki", "--", "sudo", "incus", *args],
        capture_output=True,
        text=True,
    )


def _list_containers() -> list[tuple[str, str]]:
    """Return (name, status) for every container."""
    pairs: list[tuple[str, str]] = []
    for line in _incus(["list", "--format=csv", "--columns=n,s"]).stdout.splitlines():
        name, _, status = line.partition(",")
        if name := name.strip():
            pairs.append((name, status.strip()))
    return pairs


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
        tag = f"#locki-{self.wt_id}"
        result = subprocess.run(["git", "stash", "list"], capture_output=True, text=True)
        return [line.split(":", 1)[0] for line in result.stdout.splitlines() if tag in line]

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
FLAG.2:   /(?:-[a-zA-Z]\/)?--[a-z][\w-]*(?:=(?:<[^>]+>|[^<>\s()\[\]|])+)?/
COMPOUND: /(?:<[^>]+>|[^<>\s()\[\]|])+/

%ignore /\s+/
"""


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
            short = tok[:2]
            tok = tok[3:]
        name, sep, value_text = tok.partition("=")
        value = ArgRule(value=_compound_parts(value_text)) if sep == "=" else None
        return FlagRule(short_name=short, long_name=name, value=value)

    def compound(self, c: list[Token]) -> ArgRule:
        return ArgRule(value=_compound_parts(str(c[0])))


_PARSER = Lark(_GRAMMAR, start="alt", parser="lalr", transformer=_ASTBuilder())


# ── Ruleset ──────────────────────────────────────────────────────────────────


def _extract_prefix(rule: Rule) -> tuple[str, str]:
    """Get the first two literal words from a top-level SequenceRule."""
    if not isinstance(rule, SequenceRule):
        raise ValueError("Top-level rule must be a sequence")
    words: list[str] = []
    for item in rule.sequence:
        if isinstance(item, ArgRule) and len(item.value) == 1 and isinstance(item.value[0], str):
            words.append(item.value[0])
            if len(words) == 2:
                return (words[0], words[1])
        else:
            break
    raise ValueError(f"Rule must start with two literal words, got {words}")


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
            if glued:
                flags[key] = glued
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


class Ruleset:
    def __init__(self, groups: dict[tuple[str, str], _RuleGroup]) -> None:
        self._groups = groups

    @classmethod
    def from_markdown(cls, md: str) -> Ruleset:
        """Parse every non-blank line inside ```locki-bridged-command-filter fences."""
        raw: dict[tuple[str, str], tuple[list[Rule], list[str]]] = {}
        in_block = False
        for line_raw in md.splitlines():
            line = line_raw.strip()
            if line == "```locki-bridged-command-filter":
                in_block = True
            elif in_block and line.startswith("```"):
                in_block = False
            elif in_block and line:
                rule: Rule = _PARSER.parse(line)  # pyrefly: ignore
                prefix = _extract_prefix(rule)
                entry = raw.setdefault(prefix, ([], []))
                entry[0].append(rule)
                entry[1].append(line)
        return cls({
            prefix: _RuleGroup(
                rules=rules,
                lines=lines,
                value_flag_keys=_collect_value_flag_keys(rules),
            )
            for prefix, (rules, lines) in raw.items()
        })

    def check(self, argv: list[str], wt_id: str) -> str | None:
        """Return None if allowed, or an error message."""
        if len(argv) < 2 or argv[1].startswith("-"):
            return f"Command not allowed: {shlex.join(argv)!r}"

        prefix = (argv[0], argv[1])
        group = self._groups.get(prefix)
        if group is None:
            return f"Command not allowed: {shlex.join(argv)!r}"

        positionals, flags = _split_argv(argv[2:], group.value_flag_keys)
        effective = {k: v for k, v in flags.items() if k != "--help"}
        all_positionals = [*prefix, *positionals]
        mc = MatchContext(all_positionals, effective, Context(wt_id))
        expected = set(effective)
        target = len(all_positionals)

        if any(
            p == target and used == expected
            for rule in group.rules
            for p, used in rule.match(0, frozenset(), mc)
        ):
            return None

        lines = "\n".join(f"  {line}" for line in group.lines)
        return f'Allowed forms of "{prefix[0]} {prefix[1]}" are:\n{lines}'


@cache
def _ruleset() -> Ruleset:
    return Ruleset.from_markdown((PACKAGE_DATA / "AGENTS.md").read_text())


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group("internal", hidden=True)
def internal_app() -> None:
    """Internal commands (invoked by Locki itself)."""


@internal_app.command("cleanup")
def internal_cleanup() -> None:
    """One-shot: stop idle containers, remove orphans, power off idle VM."""
    if vm_status() != "Running":
        sys.exit(1)

    try:
        last_active = json.loads(LAST_ACTIVE_FILE.read_text())
    except FileNotFoundError, json.JSONDecodeError:
        last_active = {}

    worktrees_root = WORKTREES.resolve()
    for name, _ in _list_containers():
        r = _incus(["config", "device", "get", name, "worktree", "source"])
        if r.returncode != 0 or not r.stdout.strip():
            continue
        src = pathlib.Path(r.stdout.strip()).resolve()
        if src.is_relative_to(worktrees_root) and not src.exists():
            logger.info("Deleting orphaned container %r (worktree %s is gone).", name, src)
            _incus(["delete", "--force", name])
            last_active.pop(name, None)

    running = {name for name, status in _list_containers() if status == "RUNNING"}
    active: set[str] = set()
    ops = _incus(["operation", "list", "--format=json"])
    if ops.returncode == 0 and ops.stdout.strip():
        with contextlib.suppress(json.JSONDecodeError):
            for op in json.loads(ops.stdout):
                if op.get("status") == "Running":
                    for key in ("containers", "instances"):
                        for path in (op.get("resources") or {}).get(key) or []:
                            active.add(path.rsplit("/", 1)[-1])

    now = time.time()
    stopped: set[str] = set()
    for name in running:
        if name in active or name not in last_active:
            last_active[name] = now
        elif now - last_active[name] >= IDLE_TIMEOUT:
            logger.info("Stopping idle container %r (idle %.0fs).", name, now - last_active[name])
            if _incus(["stop", name]).returncode == 0:
                stopped.add(name)
            last_active.pop(name, None)
    last_active = {n: t for n, t in last_active.items() if n in running}
    LAST_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_ACTIVE_FILE.write_text(json.dumps(last_active))

    if running - stopped:
        VM_IDLE_SINCE_FILE.unlink(missing_ok=True)
        return

    try:
        idle_since = float(VM_IDLE_SINCE_FILE.read_text())
    except FileNotFoundError, ValueError:
        idle_since = now
        VM_IDLE_SINCE_FILE.write_text(str(now))
    if now - idle_since >= VM_IDLE_TIMEOUT:
        logger.info("No running containers for %.0fs — stopping VM.", now - idle_since)
        subprocess.run([limactl(), "stop", "locki"], capture_output=True)
        VM_IDLE_SINCE_FILE.unlink(missing_ok=True)
        sys.exit(1)


@internal_app.command("daemon")
def internal_daemon() -> None:
    """Host daemon: SSH forced-command proxy + periodic cleanup."""
    log_file = STATE / "logs" / "daemon.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)

    async def main() -> None:
        HOST_KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        CLIENT_KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (HOST_KEY, CLIENT_KEY):
            if not path.exists():
                key = asyncssh.generate_private_key("ssh-ed25519")
                key.write_private_key(str(path))
                key.write_public_key(str(path.with_suffix(".pub")))
                os.chmod(path, 0o600)
        AUTHORIZED_KEYS_FILE.write_text(CLIENT_KEY.with_suffix(".pub").read_text())
        os.chmod(AUTHORIZED_KEYS_FILE, 0o600)
        RUNTIME.mkdir(parents=True, exist_ok=True)

        async def handle(process: asyncssh.SSHServerProcess) -> None:
            try:
                env = {**os.environ, "SSH_ORIGINAL_COMMAND": process.command or ""}
                sub = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "locki",
                    "internal",
                    "command-bridge",
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await process.redirect(stdin=sub.stdin, stdout=sub.stdout, stderr=sub.stderr)
                process.exit(await sub.wait() or 0)
            except Exception:
                logger.exception("SSH session failed")
                with contextlib.suppress(Exception):
                    process.exit(1)

        server = await asyncssh.listen(
            host="0.0.0.0",
            port=0,
            server_host_keys=[str(HOST_KEY)],
            authorized_client_keys=str(AUTHORIZED_KEYS_FILE),
            process_factory=handle,
            encoding=None,
            allow_scp=False,
            agent_forwarding=False,
            x11_forwarding=False,
        )
        port = next(iter(server.sockets)).getsockname()[1]
        PORT_FILE.write_text(str(port))
        PID_FILE.write_text(str(os.getpid()))
        logger.info("Locki daemon listening on 0.0.0.0:%d", port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        async def cleanup_loop() -> None:
            while not stop.is_set():
                proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "locki", "internal", "cleanup")
                if await proc.wait() != 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=CLEANUP_INTERVAL)

        cleanup_task = asyncio.create_task(cleanup_loop())
        await stop.wait()
        server.close()
        await server.wait_closed()
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    try:
        asyncio.run(main())
    finally:
        PID_FILE.unlink(missing_ok=True)
        PORT_FILE.unlink(missing_ok=True)


@internal_app.command("command-bridge | self-service")
def internal_command_bridge() -> None:
    """SSH forced command: validate and execute an allowed bridged command."""
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not cmd:
        sys.exit("No command specified.")
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        sys.exit(f"Failed to parse command: {e}")
    if len(parts) < 2:
        sys.exit("Usage: <cwd> <exe> [args...]")
    cwd_str, *argv = parts

    cwd = pathlib.Path(cwd_str).resolve()
    wt_root = WORKTREES.resolve()
    if not cwd.is_relative_to(wt_root):
        sys.exit(f"Not inside a locki worktree: {str(cwd)!r}")
    rel_parts = cwd.relative_to(wt_root).parts
    if not rel_parts:
        sys.exit(f"Not inside a locki worktree: {str(cwd)!r}")
    wt_dir = rel_parts[0]
    wt_id = wt_dir[-8:]

    # Walk up from cwd to find the .git file, then rewrite it from the trusted
    # metadata copy so a compromised container cannot redirect the gitdir.
    sandbox_root = WORKTREES / wt_dir
    p: pathlib.Path = cwd
    while True:
        if (p / ".git").is_file():
            break
        if p == sandbox_root:
            sys.exit(f"No worktree .git found at or above {str(cwd)!r}")
        p = p.parent
    rel = p.relative_to(wt_root).parts
    if len(rel) == 1:
        meta_git = WORKTREES_META / wt_dir / ".git"
    elif len(rel) == 4 and rel[1] == ".locki" and rel[2] == "include":
        meta_git = WORKTREES_META / wt_dir / "include" / rel[3] / ".git"
    else:
        sys.exit(f"Unexpected worktree layout: {'/'.join(rel)!r}")
    if not meta_git.exists():
        sys.exit(f"Missing worktree metadata: {meta_git}")
    (p / ".git").write_text(meta_git.read_text())

    if not argv:
        sys.exit("Empty command.")

    exe = pathlib.Path(argv[0]).name
    # chdir first so `gh repo view` and `git stash list` run inside the worktree.
    os.chdir(str(cwd))

    ruleset = _ruleset()
    error = ruleset.check([exe, *argv[1:]], wt_id)
    if error:
        with contextlib.suppress(OSError):
            DENIED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DENIED_LOG.open("a") as fh:
                fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{wt_id}\t{shlex.join(argv)}\n")
        sys.exit(error)

    if exe == "locki":
        os.execvp(sys.executable, [sys.executable, "-m", "locki", *argv[1:]])
    else:
        os.execvp(exe, [exe, *argv[1:]])
