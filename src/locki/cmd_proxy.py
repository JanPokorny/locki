"""TCP command proxy: executes allowed git/gh commands on behalf of sandboxed containers.

Protocol (custom framed TCP):
  Frame = <type:1B><length:4B big-endian><payload>
  Types: H=header, I=stdin, O=stdout, E=stderr, C=close-stdin, X=exit-code

Flow:
  1. Client → H frame: JSON {"argv": ["git", ...], "cwd": "/path"}
  2. Server validates argv against allowlist, cwd against managed worktrees
  3. Full-duplex: client sends I/C frames, server sends O/E frames
  4. Server → X frame (4B signed int32 exit code) on process exit
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import struct

LOCKI_HOME = pathlib.Path.home() / ".locki"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
WORKTREES_META = LOCKI_HOME / "worktrees-meta"
CMD_PROXY_PORT = 7890

# Frame types
FRAME_HEADER = ord("H")
FRAME_STDIN = ord("I")
FRAME_STDOUT = ord("O")
FRAME_STDERR = ord("E")
FRAME_CLOSE_STDIN = ord("C")
FRAME_EXIT = ord("X")


# ── allowlist DSL ─────────────────────────────────────────────────────────────

# Validators: called with str | None (None = flag absent, "" = boolean --flag).
_required = bool  # --flag=<non-empty value>


def _cmd(*spec_args, **spec_flags):
    """Build a predicate for one allowed command pattern.

    spec_args  — positional matchers: str exact, set membership, callable predicate.
    spec_flags — flag matchers (hyphens → underscores); each is called with the
                 flag's value (str) or None if absent and must return True to pass.
    --help is always permitted.
    """
    spec_flags = {"help": ..., **spec_flags}

    def match(positionals: list[str], flags: dict[str, str]) -> bool:
        if len(positionals) != len(spec_args):
            return False
        for val, spec in zip(positionals, spec_args):
            if isinstance(spec, str) and val != spec:
                return False
            if isinstance(spec, set) and val not in spec:
                return False
            if callable(spec) and not spec(val):
                return False
        for key in flags:
            if key not in spec_flags:
                return False  # unlisted flag — reject
        return all(_val_ok(flags.get(key), spec) for key, spec in spec_flags.items())

    return match


def _val_ok(val: str | None, spec) -> bool:
    if spec is ...:
        return True
    if callable(spec):
        return bool(spec(val))
    if isinstance(spec, set):
        return val in spec
    if isinstance(spec, str):
        return val == spec
    return True


# Allowlist — add a _cmd(...) line to permit a new operation.
_RULES: dict[str, list] = {
    "git": [
        _cmd("status"),
        _cmd("diff", staged=...),
        _cmd("diff", str, staged=...),
        _cmd("diff", str, str, staged=...),
        _cmd("add", all=...),
        _cmd("commit", message=_required),
        _cmd("push"),
        _cmd("fetch"),
        _cmd("log", oneline=...),
        _cmd("log", str, oneline=...),
        _cmd("show"),
        _cmd("show", str),
        _cmd("restore", str, staged=..., source=...),
    ],
    "gh": [
        _cmd("pr", "create", title=_required, body=..., base=...),
        _cmd("pr", "view"),
        _cmd("pr", "view", str.isdigit),
        _cmd("pr", "list"),
        _cmd("pr", "diff"),
        _cmd("pr", "status"),
        _cmd("run", "list"),
        _cmd("run", "view"),
        _cmd("run", "view", str.isdigit),
        _cmd("issue", "create", title=_required, body=...),
        _cmd("issue", "view"),
        _cmd("issue", "view", str.isdigit),
        _cmd("issue", "list"),
    ],
}


# ── parsing ───────────────────────────────────────────────────────────────────


def _parse(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split args into positionals and long flags.

    --flag=value  →  flags["flag"] = "value"
    --flag        →  flags["flag"] = ""       (boolean flag, empty-string sentinel)
    -x            →  ValueError  (short flags not accepted)

    Hyphens in flag names are normalised to underscores.
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    for arg in args:
        if arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            flags[key.replace("-", "_")] = value
        elif arg.startswith("-"):
            raise ValueError(
                f"Short flags are not allowed: {arg!r}. "
                "Use the long form (--flag or --flag=value)."
            )
        else:
            positionals.append(arg)
    return positionals, flags


# ── validation ────────────────────────────────────────────────────────────────


def _validate_worktree(cwd: str) -> pathlib.Path:
    wt = pathlib.Path(cwd).resolve()
    if not wt.is_relative_to(WORKTREES_HOME.resolve()):
        raise ValueError(f"Not a locki worktree: {cwd!r}")
    wt_root = WORKTREES_HOME / wt.relative_to(WORKTREES_HOME).parts[0]
    if not wt_root.is_dir():
        raise ValueError(f"Worktree does not exist: {cwd!r}")

    # Verify .git file hasn't been tampered with (prevents git hook injection).
    wt_id = wt_root.relative_to(WORKTREES_HOME).parts[0]
    meta_git = WORKTREES_META / wt_id / ".git"
    if not meta_git.exists():
        raise ValueError(f"No worktree metadata found for {wt_id!r}; re-create the worktree.")
    dot_git = wt_root / ".git"
    if not dot_git.is_file():
        raise ValueError("Worktree .git is not a file — possible tampering detected.")
    if dot_git.read_text().strip() != meta_git.read_text().strip():
        raise ValueError("Worktree .git content mismatch — possible tampering detected.")

    return wt


def _validate_command(argv: list[str]) -> tuple[str, list[str]]:
    if not argv:
        raise ValueError("Empty command")
    exe = pathlib.Path(argv[0]).name  # handle full paths like /opt/locki/bin/git
    args = argv[1:]
    if exe not in _RULES:
        raise ValueError(f"Executable {exe!r} is not allowed; use 'git' or 'gh'")
    positionals, flags = _parse(args)
    if not any(rule(positionals, flags) for rule in _RULES[exe]):
        raise ValueError(f"Command not allowed: {exe} {' '.join(args)!r}")
    return exe, args


# ── protocol helpers ──────────────────────────────────────────────────────────


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    frame_type = header[0]
    length = struct.unpack("!I", header[1:5])[0]
    payload = await reader.readexactly(length) if length else b""
    return frame_type, payload


async def write_frame(writer: asyncio.StreamWriter, frame_type: int, payload: bytes = b"") -> None:
    writer.write(bytes([frame_type]) + struct.pack("!I", len(payload)) + payload)
    await writer.drain()


# ── connection handler ────────────────────────────────────────────────────────


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        # 1. Read header frame
        frame_type, payload = await read_frame(reader)
        if frame_type != FRAME_HEADER:
            await write_frame(writer, FRAME_STDERR, b"Protocol error: expected header frame\n")
            await write_frame(writer, FRAME_EXIT, struct.pack("!i", 1))
            return

        header = json.loads(payload)
        argv = header["argv"]
        cwd = header["cwd"]

        # 2. Validate
        try:
            validated_cwd = _validate_worktree(cwd)
            exe, args = _validate_command(argv)
        except ValueError as e:
            await write_frame(writer, FRAME_STDERR, f"{e}\n".encode())
            await write_frame(writer, FRAME_EXIT, struct.pack("!i", 1))
            return

        # 3. Start subprocess
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(validated_cwd),
        )

        # 4. Full-duplex relay
        async def relay_stdin():
            try:
                while True:
                    ft, data = await read_frame(reader)
                    if ft == FRAME_STDIN:
                        if proc.stdin and not proc.stdin.is_closing():
                            proc.stdin.write(data)
                            await proc.stdin.drain()
                    elif ft == FRAME_CLOSE_STDIN:
                        if proc.stdin and not proc.stdin.is_closing():
                            proc.stdin.close()
                        break
            except (asyncio.IncompleteReadError, ConnectionError):
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()

        async def relay_stdout():
            assert proc.stdout is not None
            while True:
                data = await proc.stdout.read(8192)
                if not data:
                    break
                await write_frame(writer, FRAME_STDOUT, data)

        async def relay_stderr():
            assert proc.stderr is not None
            while True:
                data = await proc.stderr.read(8192)
                if not data:
                    break
                await write_frame(writer, FRAME_STDERR, data)

        await asyncio.gather(relay_stdin(), relay_stdout(), relay_stderr())

        # 5. Send exit code
        returncode = await proc.wait()
        await write_frame(writer, FRAME_EXIT, struct.pack("!i", returncode))

    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    except Exception as e:
        with contextlib.suppress(Exception):
            await write_frame(writer, FRAME_STDERR, f"Server error: {e}\n".encode())
            await write_frame(writer, FRAME_EXIT, struct.pack("!i", 1))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def serve() -> None:
    LOCKI_HOME.mkdir(parents=True, exist_ok=True)
    pid_file = LOCKI_HOME / "cmd-proxy.pid"
    pid_file.write_text(str(os.getpid()))
    server = await asyncio.start_server(handle_connection, "0.0.0.0", CMD_PROXY_PORT)
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
