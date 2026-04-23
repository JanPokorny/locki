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


# ── Tokenizer ────────────────────────────────────────────────────────────────
#
# A grammar line is tokenized into a flat list of strings.  Structural tokens
# are `(`, `)`, `[`, `]`, `|`, `...`; everything else is a flag declaration
# (`--flag`, `--flag=<val>`, `-x/--flag`, `-x/--flag=<val>`) or a compound
# positional (literal text with embedded `<placeholder>`s).  The matcher walks
# the flat list directly, using a precomputed bracket-pair map — no AST.

_COMPOUND_BODY = r"(?:<[^>]+>|[^<>\s()\[\]|])+"
_TOKEN_RE = re.compile(rf"\.\.\.|(?:-[a-zA-Z]/)?--[a-z][\w-]*(?:={_COMPOUND_BODY})?|[()|\[\]]|{_COMPOUND_BODY}")
_COMPOUND_PART_RE = re.compile(r"<([^>]+)>|([^<>]+)")


def _compound_parts(text: str) -> list[CompoundPart]:
    return [
        Placeholder(m.group(1)) if m.group(1) is not None else Literal(m.group(2))
        for m in _COMPOUND_PART_RE.finditer(text)
    ]


def _pair_map(tokens: list[str]) -> dict[int, int]:
    """Map each `(` / `[` index to its matching `)` / `]` index."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for i, tok in enumerate(tokens):
        if tok in "([":
            stack.append(i)
        elif tok in ")]":
            if not stack:
                raise ValueError(f"Unmatched {tok!r}: {tokens!r}")
            pairs[stack.pop()] = i
    if stack:
        raise ValueError(f"Unclosed {tokens[stack[0]]!r}: {tokens!r}")
    return pairs


# ── Matcher ──────────────────────────────────────────────────────────────────
#
# Every helper yields all successful continuations of a match; backtracking
# falls out of `yield from`.  `_match_alts` handles top-level `|`, `_match_seq`
# concatenation, `_match_item` one item (with optional `...` postfix),
# `_match_atom` flag/compound leaves.


def _match_alts(
    tokens: list[str], pairs: dict[int, int], i: int, end: int, pos: int, used: frozenset[str], mc: MatchContext
) -> Iterator[tuple[int, frozenset[str]]]:
    starts = [i]
    depth = 0
    for j in range(i, end):
        t = tokens[j]
        if t in "([":
            depth += 1
        elif t in ")]":
            depth -= 1
        elif t == "|" and depth == 0:
            starts.append(j + 1)
    alt_ends = [starts[k + 1] - 1 for k in range(len(starts) - 1)] + [end]
    for s, e in zip(starts, alt_ends, strict=True):
        yield from _match_seq(tokens, pairs, s, e, pos, used, mc)


def _match_seq(
    tokens: list[str], pairs: dict[int, int], i: int, end: int, pos: int, used: frozenset[str], mc: MatchContext
) -> Iterator[tuple[int, frozenset[str]]]:
    if i >= end:
        yield pos, used
        return
    for ni, p2, u2 in _match_item(tokens, pairs, i, pos, used, mc):
        yield from _match_seq(tokens, pairs, ni, end, p2, u2, mc)


def _match_item(
    tokens: list[str], pairs: dict[int, int], i: int, pos: int, used: frozenset[str], mc: MatchContext
) -> Iterator[tuple[int, int, frozenset[str]]]:
    """Yield (next_i, pos, used) for every way to match ONE item at tokens[i]."""
    tok = tokens[i]
    if tok == "[":
        close = pairs[i]
        after_base = close + 1

        def once(p: int, u: frozenset[str]) -> Iterator[tuple[int, frozenset[str]]]:
            yield p, u  # optional: skip
            yield from _match_alts(tokens, pairs, i + 1, close, p, u, mc)
    elif tok == "(":
        close = pairs[i]
        after_base = close + 1

        def once(p: int, u: frozenset[str]) -> Iterator[tuple[int, frozenset[str]]]:
            yield from _match_alts(tokens, pairs, i + 1, close, p, u, mc)
    else:
        after_base = i + 1

        def once(p: int, u: frozenset[str]) -> Iterator[tuple[int, frozenset[str]]]:
            yield from _match_atom(tok, p, u, mc)

    if after_base < len(tokens) and tokens[after_base] == "...":
        # `...` is "one or more": yield only after each successful match of `once`.
        after = after_base + 1

        def go(p: int, u: frozenset[str]) -> Iterator[tuple[int, int, frozenset[str]]]:
            for p2, u2 in once(p, u):
                yield after, p2, u2
                yield from go(p2, u2)

        yield from go(pos, used)
    else:
        for p2, u2 in once(pos, used):
            yield after_base, p2, u2


def _match_atom(
    tok: str, pos: int, used: frozenset[str], mc: MatchContext
) -> Iterator[tuple[int, frozenset[str]]]:
    """Match a flag or compound-positional leaf token."""
    # Strip `-x/` short-alias prefix — split_argv already normalized short→long.
    if tok.startswith("-") and not tok.startswith("--"):
        tok = tok[3:]
    if tok.startswith("--") and "=" in tok:
        name, _, val_text = tok[2:].partition("=")
        key = name.replace("-", "_")
        val = mc.flags.get(key)
        if key not in used and val is not None and mc.ctx.compound(_compound_parts(val_text)).fullmatch(val):
            yield pos, used | {key}
    elif tok.startswith("--"):
        key = tok[2:].replace("-", "_")
        if key not in used and mc.flags.get(key) == "":
            yield pos, used | {key}
    elif pos < len(mc.positionals) and mc.ctx.compound(_compound_parts(tok)).fullmatch(mc.positionals[pos]):
        yield pos + 1, used


# ── Ruleset ──────────────────────────────────────────────────────────────────


class Ruleset:
    def __init__(self, rules: list[tuple[list[str], dict[int, int]]]) -> None:
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
        rules: list[tuple[list[str], dict[int, int]]] = []
        for line in lines:
            tokens = _TOKEN_RE.findall(line)
            rules.append((tokens, _pair_map(tokens)))
        return cls(rules)

    @cached_property
    def _flag_index(self) -> tuple[frozenset[str], dict[str, str]]:
        """Discover every flag declared in the grammar: (value-flag long keys, short→long)."""
        value_keys: set[str] = set()
        short_aliases: dict[str, str] = {}
        for tokens, _ in self.rules:
            for tok in tokens:
                short: str | None = None
                if tok.startswith("-") and not tok.startswith("--"):
                    short = tok[1]
                    tok = tok[3:]
                if not tok.startswith("--"):
                    continue
                name, sep, _ = tok[2:].partition("=")
                key = name.replace("-", "_")
                if sep == "=":
                    value_keys.add(key)
                if short is not None:
                    prior = short_aliases.get(short)
                    if prior is not None and prior != key:
                        raise ValueError(f"Short flag -{short} maps to both --{prior} and --{name}")
                    short_aliases[short] = key
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
            p == target and used == expected
            for tokens, pairs in self.rules
            for p, used in _match_alts(tokens, pairs, 0, len(tokens), 0, frozenset(), mc)
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
    if not cwd.is_relative_to(WORKTREES.resolve()):
        sys.exit(f"Not a locki worktree: {cwd_str!r}")
    wt_root = WORKTREES / cwd.relative_to(WORKTREES).parts[0]
    wt_id = wt_root.name
    meta_git = WORKTREES_META / wt_id / ".git"
    dot_git = wt_root / ".git"
    if not wt_root.is_dir() or not meta_git.exists() or not dot_git.is_file():
        sys.exit(f"Invalid worktree: {cwd_str!r}")
    if dot_git.read_text().strip() != meta_git.read_text().strip():
        sys.exit("Worktree .git mismatch — possible tampering.")
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
