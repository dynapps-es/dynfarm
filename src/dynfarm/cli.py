"""Entry point: the `dynfarm` command group.

Subcommands are loaded lazily (`LazyGroup`): `dynfarm --help` or `dynfarm --version` must not pay for importing
`httpx`, `keyring` and every command module, because people type them often and CI calls them in tight loops. Each
entry of `COMMANDS` is `name -> "module:attribute"`, imported only when that command runs or its help is shown.
"""

from __future__ import annotations

import importlib
import sys

import click

from dynfarm import __version__
from dynfarm._brand import NAME, env
from dynfarm.context import AppContext

#: name -> "module:attribute" of a click command or group. Order here is the order in `--help`.
COMMANDS: dict[str, str] = {
    "auth": "dynfarm.commands.auth:auth",
    "whoami": "dynfarm.commands.identity:whoami",
    "plugin": "dynfarm.commands.identity:plugin",
    "callback-key": "dynfarm.commands.identity:callback_key",
    "task": "dynfarm.commands.task:task",
    "agent": "dynfarm.commands.agent:agent",
    "schedule": "dynfarm.commands.schedule:schedule",
    "credential": "dynfarm.commands.credential:credential",
    "api": "dynfarm.commands.api:api",
    "completion": "dynfarm.commands.completion:completion",
}


class LazyGroup(click.Group):
    """A click group whose subcommands are imported on first use."""

    def __init__(self, *args, lazy_commands: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lazy_commands = dict(lazy_commands or {})

    def list_commands(self, ctx: click.Context) -> list[str]:
        return [*super().list_commands(ctx), *self.lazy_commands]

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name in self.lazy_commands:
            module_name, _, attribute = self.lazy_commands[cmd_name].partition(":")
            return getattr(importlib.import_module(module_name), attribute)
        return super().get_command(ctx, cmd_name)


@click.group(cls=LazyGroup, lazy_commands=COMMANDS, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "--version", prog_name=NAME, message="%(prog)s %(version)s")
@click.option(
    "--debug",
    is_flag=True,
    envvar=env("DEBUG"),
    help="Print method, path, status and timing of each request to stderr (never headers or bodies).",
)
@click.pass_context
def cli(ctx: click.Context, debug: bool) -> None:
    """Work with the Agent Farm from the command line."""
    # `obj` may already be an AppContext: tests pass one carrying the fake farm's transport.
    ctx.ensure_object(AppContext).debug = debug


def _survive_a_narrow_terminal() -> None:
    """Make what the farm sends printable on any standard output.

    A piped standard output on Windows (and a POSIX one under `LANG=C`) is encoded with the machine's legacy code page,
    which cannot hold every character a task log or a name may contain: Python would stop with a traceback after the
    command had worked. A character the encoding lacks is written as an escape instead (`\\u65e5`), as standard error
    already does. A UTF-8 output is untouched.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)  # absent on a replaced stream (tests, embedding)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")


def main() -> None:
    _survive_a_narrow_terminal()
    cli(prog_name=NAME)
