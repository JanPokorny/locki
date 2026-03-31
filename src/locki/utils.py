import contextlib
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from io import BytesIO

import anyio
import anyio.abc
from anyio import create_task_group
from anyio.abc import ByteReceiveStream, TaskGroup
from rich.console import Capture
from rich.text import Text

from locki.console import console


async def _receive_stream(stream: ByteReceiveStream, buffer: BytesIO):
    async for chunk in stream:
        console.print(Text.from_ansi(chunk.decode(errors="replace")), style="dim")
        buffer.write(chunk)


@asynccontextmanager
async def capture_output(
    process: anyio.abc.Process,
    stdout_buf: BytesIO,
    stderr_buf: BytesIO,
) -> AsyncIterator[TaskGroup]:
    async with create_task_group() as tg:
        if process.stdout:
            tg.start_soon(_receive_stream, process.stdout, stdout_buf)
        if process.stderr:
            tg.start_soon(_receive_stream, process.stderr, stderr_buf)
        yield tg


async def run_command(
    command: list[str],
    message: str,
    env: dict[str, str] | None = None,
    cwd: str = ".",
    check: bool = True,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = env or {}
    try:
        with status(message):
            console.print(f"Command: {command}", style="dim")
            start_time = time.time()
            async with await anyio.open_process(
                command,
                stdin=subprocess.PIPE if input else subprocess.DEVNULL,
                env={**os.environ, **env},
                cwd=cwd,
            ) as process:
                stdout_buf, stderr_buf = BytesIO(), BytesIO()
                async with capture_output(process, stdout_buf, stderr_buf):
                    if process.stdin and input:
                        await process.stdin.send(input)
                        await process.stdin.aclose()
                    await process.wait()

                if check and process.returncode != 0:
                    raise subprocess.CalledProcessError(
                        process.returncode or 0,
                        command,
                        stdout_buf.getvalue(),
                        stderr_buf.getvalue(),
                    )

                elapsed = int(time.time() - start_time)
                duration = (
                    "" if elapsed < 5 else f"({elapsed}s)" if elapsed < 60 else f"({elapsed // 60}m{elapsed % 60}s)"
                )

                if SHOW_SUCCESS_STATUS.get():
                    console.print(f"{message} [[green]DONE[/green]] [dim]{duration}[/dim]")
                return subprocess.CompletedProcess(
                    command, process.returncode or 0, stdout_buf.getvalue(), stderr_buf.getvalue()
                )
    except FileNotFoundError:
        console.print(f"{message} [[red]ERROR[/red]]")
        console.error(f"{command[0]} is not installed. Please install it first.")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        console.print(f"{message} [[red]ERROR[/red]]")
        console.print(f"[red]Exit code: {e.returncode}[/red]")
        if e.stderr:
            console.print(f"[red]Stderr: {e.stderr.decode(errors='replace').strip()}[/red]")
        raise


IN_VERBOSITY_CONTEXT: ContextVar[bool] = ContextVar("in_verbosity_context", default=False)
VERBOSE: ContextVar[bool] = ContextVar("verbose", default=False)
SHOW_SUCCESS_STATUS: ContextVar[bool] = ContextVar("show_success_status", default=True)


@contextlib.contextmanager
def status(message: str):
    if VERBOSE.get():
        console.print(f"{message}...")
        yield
    elif SHOW_SUCCESS_STATUS.get():
        console.print(f"\n[bold]{message}[/bold]")
        with console.status(f"{message}...", spinner="dots"):
            yield
    else:
        console.print(f"\n[bold]{message}[/bold]")
        yield


@contextlib.contextmanager
def verbosity(verbose: bool, show_success_status: bool = True):
    if IN_VERBOSITY_CONTEXT.get():
        yield
        return

    IN_VERBOSITY_CONTEXT.set(True)
    token_verbose = VERBOSE.set(verbose)
    token_status = SHOW_SUCCESS_STATUS.set(show_success_status)
    capture: Capture | None = None
    try:
        with console.capture() if not verbose else contextlib.nullcontext() as capture:
            yield
    except Exception:
        if not verbose and capture and (logs := capture.get().strip()):
            console.print("\n[yellow]--- Captured logs ---[/yellow]\n")
            console.print(Text.from_ansi(logs, style="dim"))
            console.print("\n[red]------- Error -------[/red]\n")
        raise
    finally:
        VERBOSE.reset(token_verbose)
        IN_VERBOSITY_CONTEXT.set(False)
        SHOW_SUCCESS_STATUS.reset(token_status)
