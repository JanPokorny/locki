import asyncio
import functools
import inspect
import logging
import sys

import typer
from typer.core import TyperGroup

logger = logging.getLogger(__name__)


class AsyncTyper(typer.Typer):
    def command(self, *args, **kwargs):
        parent_decorator = super().command(*args, **kwargs)

        def decorator(f):
            @functools.wraps(f)
            def wrapped_f(*args, **kwargs):
                if sys.stdout.isatty():
                    sys.stdout.write("\x1b[>0u")
                    sys.stdout.flush()
                try:
                    if inspect.iscoroutinefunction(f):
                        return asyncio.run(f(*args, **kwargs))
                    else:
                        return f(*args, **kwargs)
                except* Exception as eg:
                    for exc in eg.exceptions:
                        logger.error("%s: %s", type(exc).__name__, exc)
                    sys.exit(1)
                finally:
                    if sys.stdout.isatty():
                        sys.stdout.write("\x1b[<u")
                        sys.stdout.flush()

            parent_decorator(wrapped_f)
            return f

        return decorator


class AliasGroup(TyperGroup):
    """Support comma/pipe-separated command name aliases, e.g. 'start|up'."""

    def get_command(self, ctx, cmd_name):
        for cmd in self.commands.values():
            if cmd.name and cmd_name in cmd.name.replace(" ", "").split(","):
                cmd_name = cmd.name
                break
        return super().get_command(ctx, cmd_name)


class AsyncTyperWithAliases(AsyncTyper):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("cls", AliasGroup)
        super().__init__(*args, **kwargs)
