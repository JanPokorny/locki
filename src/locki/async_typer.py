import asyncio
import functools
import inspect
import logging
import sys

import typer
from typer.core import TyperGroup

logger = logging.getLogger(__name__)


class AsyncTyper(typer.Typer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, cls=AliasGroup, **kwargs)

    def command(self, *args, **kwargs):
        parent_decorator = super().command(*args, **kwargs)

        def decorator(f):
            @functools.wraps(f)
            def wrapped_f(*args, **kwargs):
                try:
                    if inspect.iscoroutinefunction(f):
                        return asyncio.run(f(*args, **kwargs))
                    else:
                        return f(*args, **kwargs)
                except* Exception as eg:
                    for exc in eg.exceptions:
                        logger.error("%s: %s", type(exc).__name__, exc)
                    sys.exit(1)

            parent_decorator(wrapped_f)
            return f

        return decorator


class AliasGroup(TyperGroup):
    def get_command(self, ctx, cmd_name):
        return super().get_command(
            ctx,
            next(
                (cmd.name for cmd in self.commands.values() if cmd.name and cmd_name in cmd.name.split(" | ")), cmd_name
            ),
        )
