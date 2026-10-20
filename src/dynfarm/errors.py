"""Exit codes, the errors every command can raise, and the one wrapper that turns them into a message and an exit code.

Why this is a module of its own: every command ends the same way, and a script that drives `dynfarm` branches on the
exit code, so the mapping must live in one place instead of in each command:

    0  ok
    1  failure: the farm answered an error, or a task did not succeed with `--exit-status`
    2  usage or configuration: no farm configured, a refused `http://` host, a bad flag
    4  authentication needed: no token, `token_invalid`, `token_expired` (gh uses 4 for the same thing)

`token_expired` and `token_invalid` both exit 4 but say different things (`farm-api.md` §3.1): expired means "get a new
one and log in again", invalid means "stop, this key is not accepted". A message never carries a token: the farm's own
text is passed through, nothing the tool knows about the credential is added to it.

This module is imported by `config`, which every command needs, so it imports nothing heavy (no `httpx`).
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable
from typing import Any, TypeVar

import click

from dynfarm._brand import NAME

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_AUTH = 4

F = TypeVar("F", bound=Callable[..., Any])


class DynfarmError(Exception):
    """An error the tool reports itself, as `error: <message>` plus an exit code. Anything else is a bug."""

    exit_code = EXIT_FAILURE

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def render(self) -> list[str]:
        """The lines printed to stderr."""
        return [f"error: {self.message}"]


class ConfigError(DynfarmError):
    """Something the person has to fix before the command can run: no farm, a refused host, a broken config file."""

    exit_code = EXIT_USAGE


class AuthRequired(DynfarmError):
    """No token could be found for the farm. Distinct from a token the farm refused, which is a `FarmError`."""

    exit_code = EXIT_AUTH


class FarmError(DynfarmError):
    """The farm answered an error, or could not be reached.

    `code` is the farm's stable `error.code` (`farm-api.md` §15) and is what callers branch on, never `message`.
    `retryable` is the farm's own word on whether repeating the same request can work. `retry_after` is the farm's
    `Retry-After`, in seconds, as sent (the client caps what it waits, not what it reports).
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
        correlation_id: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.retry_after = retry_after
        self.correlation_id = correlation_id

    @property
    def exit_code(self) -> int:  # type: ignore[override]
        return EXIT_AUTH if self.status == 401 else EXIT_FAILURE

    def render(self) -> list[str]:
        if self.code == "token_expired":
            text = f"your token expired — get a new one and run `{NAME} auth login`"
        elif self.code == "token_invalid":
            text = "the farm rejected your token"
        else:
            text = self.message
        lines = [f"error: {text} ({self.code})"]
        if self.retryable:
            if self.retry_after is not None:
                lines.append(f"retryable: try again in {math.ceil(self.retry_after)} s")
            else:
                lines.append("retryable: try again")
        return lines


def handle(fn: F) -> F:
    """Wrap a click command callback: a `DynfarmError` becomes its message on stderr and its exit code.

    Anything that is not a `DynfarmError` is left alone on purpose: a traceback is the right answer to a bug, and
    swallowing it behind "error: something went wrong" would hide it from the person who can fix it.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except DynfarmError as exc:
            for line in exc.render():
                click.echo(line, err=True)
            raise click.exceptions.Exit(exc.exit_code) from None

    return wrapper  # type: ignore[return-value]
