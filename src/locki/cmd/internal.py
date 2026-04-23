"""Internal commands invoked by Locki itself — not for direct end-user use.

* `locki internal cleanup` — one-shot: stop idle containers, remove orphans, power off idle VM.
* `locki internal daemon`  — long-running host daemon: asyncssh forced-command proxy + cleanup scheduler.
* `locki internal self-service` — SSH forced command handler: validate and run a whitelisted command.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import importlib.resources
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
from functools import cached_property

import asyncssh
import click
from lark import Lark, Transformer

from locki.paths import DATA, DENIED_LOG, RUNTIME, STATE, WORKTREES, WORKTREES_META
from locki.utils import limactl

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 600
VM_IDLE_TIMEOUT = 600
CLEANUP_INTERVAL = 60

LAST_ACTIVE_FILE = STATE / "cleanup" / "last-active.json"
VM_IDLE_SINCE_FILE = STATE / "cleanup" / "vm-idle-since"
HOST_KEY = STATE / "ssh" / "host_key"
CLIENT_KEY = DATA / "home" / ".ssh" / "id_locki"
AUTHORIZED_KEYS_FILE = STATE / "ssh" / "authorized_keys"
PID_FILE = RUNTIME / "daemon.pid"
PORT_FILE = RUNTIME / "daemon.port"


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


# ── Self-service grammar engine ───────────────────────────────────────────────


@dataclass
class Literal:
    """Literal text segment inside a compound token."""

    text: str


@dataclass
class Placeholder:
    """A `<name>` segment inside a compound token."""

    name: str


CompoundPart = Literal | Placeholder


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
        return re.compile("".join(buf), re.DOTALL)


@dataclass
class MatchContext:
    positionals: list[str]
    flags: dict[str, str]
    ctx: Context


# ── Grammar ──────────────────────────────────────────────────────────────────
#
# Each line is parsed with Lark into an AST of `Alt`/`Seq`/`Item` nodes whose
# leaves are `Flag` or `Positional`.  An `Item` carries the `[...]` (optional)
# and `...` (one-or-more) modifiers so the matcher can treat both uniformly.


@dataclass
class Alt:
    options: list[Seq]


@dataclass
class Seq:
    items: list[Item]


@dataclass
class Flag:
    key: str
    short: str | None
    value: list[CompoundPart] | None  # None for bool flags.


@dataclass
class Positional:
    parts: list[CompoundPart]


@dataclass
class Item:
    body: Alt | Flag | Positional
    optional: bool
    repeat: bool


_COMPOUND_PART_RE = re.compile(r"<([^>]+)>|([^<>]+)")


def _compound_parts(text: str) -> list[CompoundPart]:
    return [
        Placeholder(m.group(1)) if m.group(1) is not None else Literal(m.group(2))
        for m in _COMPOUND_PART_RE.finditer(text)
    ]


_GRAMMAR = r"""
alt: seq ("|" seq)*
seq: item*
item: atom ELLIPSIS?
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


@dataclass
class _OptWrapper:
    """Sentinel used only while building the AST — unwrapped by `_ASTBuilder.item`."""

    body: Alt


class _ASTBuilder(Transformer):
    def alt(self, c: list[Seq]) -> Alt:
        return Alt(list(c))

    def seq(self, c: list[Item]) -> Seq:
        return Seq(list(c))

    def item(self, c: list[Alt | Flag | Positional | _OptWrapper]) -> Item:
        atom = c[0]
        repeat = len(c) > 1
        if isinstance(atom, _OptWrapper):
            return Item(atom.body, optional=True, repeat=repeat)
        return Item(atom, optional=False, repeat=repeat)

    def group(self, c: list[Alt]) -> Alt:
        return c[0]

    def opt(self, c: list[Alt]) -> _OptWrapper:
        return _OptWrapper(c[0])

    def flag(self, c: list[str]) -> Flag:
        tok = str(c[0])
        short: str | None = None
        if tok.startswith("-") and not tok.startswith("--"):
            short = tok[1]
            tok = tok[3:]
        name, sep, value_text = tok[2:].partition("=")
        value = _compound_parts(value_text) if sep == "=" else None
        return Flag(key=name.replace("-", "_"), short=short, value=value)

    def compound(self, c: list[str]) -> Positional:
        return Positional(parts=_compound_parts(str(c[0])))


_PARSER = Lark(_GRAMMAR, start="alt", parser="lalr", transformer=_ASTBuilder())


# ── Matcher ──────────────────────────────────────────────────────────────────
#
# Each helper yields `(pos, used)` for every way to match the node; backtracking
# falls out of `yield from`.  `_match` dispatches on node type, `_match_seq`
# concatenates, `_match_item` handles `[...]` and `...` modifiers.


def _match(
    node: Alt | Seq | Item | Flag | Positional, pos: int, used: frozenset[str], mc: MatchContext
) -> Iterator[tuple[int, frozenset[str]]]:
    if isinstance(node, Alt):
        for seq in node.options:
            yield from _match(seq, pos, used, mc)
    elif isinstance(node, Seq):
        yield from _match_seq(node.items, 0, pos, used, mc)
    elif isinstance(node, Item):
        yield from _match_item(node, pos, used, mc)
    elif isinstance(node, Flag):
        if node.key in used:
            return
        val = mc.flags.get(node.key)
        if node.value is None:
            if val == "":
                yield pos, used | {node.key}
        elif val is not None and mc.ctx.compound(node.value).fullmatch(val):
            yield pos, used | {node.key}
    elif pos < len(mc.positionals) and mc.ctx.compound(node.parts).fullmatch(mc.positionals[pos]):
        yield pos + 1, used


def _match_seq(
    items: list[Item], i: int, pos: int, used: frozenset[str], mc: MatchContext
) -> Iterator[tuple[int, frozenset[str]]]:
    if i >= len(items):
        yield pos, used
        return
    for p2, u2 in _match(items[i], pos, used, mc):
        yield from _match_seq(items, i + 1, p2, u2, mc)


def _match_item(item: Item, pos: int, used: frozenset[str], mc: MatchContext) -> Iterator[tuple[int, frozenset[str]]]:
    def once(p: int, u: frozenset[str]) -> Iterator[tuple[int, frozenset[str]]]:
        if item.optional:
            yield p, u
        yield from _match(item.body, p, u, mc)

    if item.repeat:
        # `...` is "one or more": yield only after each successful match.
        def go(p: int, u: frozenset[str]) -> Iterator[tuple[int, frozenset[str]]]:
            for p2, u2 in once(p, u):
                yield p2, u2
                yield from go(p2, u2)

        yield from go(pos, used)
    else:
        yield from once(pos, used)


def _walk_flags(node: Alt | Seq | Item | Flag | Positional) -> Iterator[Flag]:
    if isinstance(node, Flag):
        yield node
    elif isinstance(node, Alt):
        for seq in node.options:
            yield from _walk_flags(seq)
    elif isinstance(node, Seq):
        for item in node.items:
            yield from _walk_flags(item)
    elif isinstance(node, Item):
        yield from _walk_flags(node.body)


# ── Ruleset ──────────────────────────────────────────────────────────────────


class Ruleset:
    def __init__(self, rules: list[Alt]) -> None:
        self.rules = rules

    @classmethod
    def from_markdown(cls, md: str) -> Ruleset:
        """Parse every non-blank line inside ```locki-self-service-command-filter fences as a grammar rule."""
        rules: list[Alt] = []
        in_block = False
        for raw in md.splitlines():
            line = raw.strip()
            if line == "```locki-self-service-command-filter":
                in_block = True
            elif in_block and line.startswith("```"):
                in_block = False
            elif in_block and line:
                rules.append(_PARSER.parse(line))  # pyrefly: ignore
        return cls(rules)

    @cached_property
    def _flag_index(self) -> tuple[frozenset[str], dict[str, str]]:
        """Discover every flag declared in the grammar: (value-flag long keys, short→long)."""
        value_keys: set[str] = set()
        short_aliases: dict[str, str] = {}
        for rule in self.rules:
            for flag in _walk_flags(rule):
                if flag.value is not None:
                    value_keys.add(flag.key)
                if flag.short is not None:
                    prior = short_aliases.get(flag.short)
                    if prior is not None and prior != flag.key:
                        raise ValueError(f"Short flag -{flag.short} maps to both --{prior} and --{flag.key}")
                    short_aliases[flag.short] = flag.key
        return frozenset(value_keys), short_aliases

    @property
    def value_flag_keys(self) -> frozenset[str]:
        return self._flag_index[0]

    @property
    def short_aliases(self) -> dict[str, str]:
        return self._flag_index[1]

    def split_argv(self, args: list[str]) -> tuple[list[str], dict[str, str]]:
        """Split argv into positionals and long flags.

        Short flags registered in the grammar (`-x/--long`) are normalized to their
        long key.  For value-flags, `--flag value`, `--flag=value`, `-x value`, `-xvalue`
        and `-x=value` all work; bool flags are standalone.
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
                key, sep, value = arg[2:].partition("=")
                key = key.replace("-", "_")
                if sep == "" and key in self.value_flag_keys and i + 1 < len(args) and not args[i + 1].startswith("-"):
                    value = args[i + 1]
                    i += 1
                flags[key] = value
            elif len(arg) >= 2 and arg[0] == "-":
                short = arg[1]
                if short not in self.short_aliases:
                    raise ValueError(f"Unknown short flag: {arg!r}")
                key = self.short_aliases[short]
                glued = arg[2:].removeprefix("=")
                if glued:
                    if key not in self.value_flag_keys:
                        raise ValueError(f"Short flag -{short} does not take a value: {arg!r}")
                    flags[key] = glued
                elif key in self.value_flag_keys and i + 1 < len(args) and not args[i + 1].startswith("-"):
                    flags[key] = args[i + 1]
                    i += 1
                else:
                    flags[key] = ""
            else:
                positionals.append(arg)
            i += 1
        return positionals, flags

    def is_allowed(self, positionals: list[str], flags: dict[str, str], wt_id: str) -> bool:
        """`--help` is always allowed; every other flag must be consumed by the matching rule."""
        effective = {k: v for k, v in flags.items() if k != "help"}
        mc = MatchContext(positionals, effective, Context(wt_id))
        expected = set(effective)
        target = len(positionals)
        return any(
            p == target and used == expected for rule in self.rules for p, used in _match(rule, 0, frozenset(), mc)
        )


RULESET = Ruleset.from_markdown((importlib.resources.files("locki") / "data" / "AGENTS.md").read_text())


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group("internal", hidden=True)
def internal_app() -> None:
    """Internal commands (invoked by Locki itself)."""


@internal_app.command("cleanup")
def internal_cleanup() -> None:
    """One-shot: stop idle containers, remove orphans, power off idle VM."""
    lines = subprocess.run([limactl(), "list", "--json"], capture_output=True, text=True).stdout.splitlines()
    for line in lines:
        with contextlib.suppress(json.JSONDecodeError):
            vm = json.loads(line)
            if vm.get("name") == "locki" and vm.get("status") == "Running":
                break
    else:
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
    for name in running:
        if name in active or name not in last_active:
            last_active[name] = now
        elif now - last_active[name] >= IDLE_TIMEOUT:
            logger.info("Stopping idle container %r (idle %.0fs).", name, now - last_active[name])
            _incus(["stop", name])
            last_active.pop(name, None)
    last_active = {n: t for n, t in last_active.items() if n in running}
    LAST_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_ACTIVE_FILE.write_text(json.dumps(last_active))

    if any(status == "RUNNING" for _, status in _list_containers()):
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
                    "self-service",
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


def _locate_worktree(cwd: pathlib.Path) -> tuple[str, pathlib.Path, pathlib.Path]:
    """Walk up from *cwd* to find the nearest `.git` file and identify its sandbox.

    Returns `(wt_id, dot_git_path, meta_git_path)`.  `wt_id` is the *parent* sandbox id
    even when cwd is inside an include — that keeps branch/stash ownership rules
    consistent across the whole sandbox.  Exits on any invariant violation.
    """
    wt_root = WORKTREES.resolve()
    if not cwd.is_relative_to(wt_root):
        sys.exit(f"Not inside a locki worktree: {str(cwd)!r}")
    parts = cwd.relative_to(wt_root).parts
    if not parts:
        sys.exit(f"Not inside a locki worktree: {str(cwd)!r}")

    wt_id = parts[0]
    sandbox_root = WORKTREES / wt_id

    # Walk up from cwd towards the sandbox root, stopping at the first `.git` file.
    p: pathlib.Path = cwd
    while True:
        candidate = p / ".git"
        if candidate.is_file():
            break
        if p == sandbox_root:
            sys.exit(f"No worktree .git found at or above {str(cwd)!r}")
        p = p.parent

    # Map the found .git back to its expected meta location.
    rel = p.relative_to(wt_root).parts
    if len(rel) == 1:
        meta_git = WORKTREES_META / wt_id / ".git"
    elif len(rel) == 4 and rel[1] == ".locki" and rel[2] == "includes":
        meta_git = WORKTREES_META / wt_id / "includes" / rel[3] / ".git"
    else:
        sys.exit(f"Unexpected worktree layout: {'/'.join(rel)!r}")

    if not meta_git.exists():
        sys.exit(f"Missing worktree metadata: {meta_git}")
    if p.joinpath(".git").read_text().strip() != meta_git.read_text().strip():
        sys.exit("Worktree .git mismatch — possible tampering.")
    return wt_id, p / ".git", meta_git


@internal_app.command("self-service")
def internal_self_service() -> None:
    """SSH forced command: validate and execute an allowed self-service command."""
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
    wt_id, _dot_git, _meta_git = _locate_worktree(cwd)
    if not argv:
        sys.exit("Empty command.")

    exe = pathlib.Path(argv[0]).name
    try:
        positionals, flags = RULESET.split_argv(argv[1:])
    except ValueError as e:
        sys.exit(str(e))

    # chdir first so `gh repo view` and `git stash list` run inside the worktree.
    os.chdir(str(cwd))

    if not RULESET.is_allowed([exe, *positionals], flags, wt_id):
        with contextlib.suppress(OSError):
            DENIED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DENIED_LOG.open("a") as fh:
                fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{wt_id}\t{shlex.join(argv)}\n")
        sys.exit(f"Command not allowed: {' '.join(argv)!r}")

    if exe == "locki":
        os.execvp(sys.executable, [sys.executable, "-m", "locki", *argv[1:]])
    else:
        os.execvp(exe, [exe, *argv[1:]])
