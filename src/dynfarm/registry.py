"""Which command calls which API operation, in one place, so a test can notice an operation nobody wired.

A command declares the `operationId`s of the OpenAPI it calls:

    @task.command("list")
    @operation("list_tasks")
    @needs_client
    def list_(client, ...): ...

The parity test (`tests/test_parity.py`) compares `registered()` with the `operationId`s of
`tests/fixtures/openapi.json` and of the live API, and fails when one has neither a command nor an explicit
exclusion. That is what turns "the API grew an endpoint" into a red CI job instead of a silent gap in the CLI.

Commands are imported lazily (`cli.COMMANDS`), so `registered()` only sees the modules already imported;
`load_all_commands()` imports them all, which is what the parity test needs.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

#: operationId -> dotted names of the command callbacks that call it.
_OPERATIONS: dict[str, set[str]] = {}

#: Attribute set on a callback, so the declaration survives `functools.wraps` in either decorator order.
ATTRIBUTE = "__dynfarm_operations__"


def operation(*operation_ids: str) -> Callable[[F], F]:
    """Record that the decorated command callback calls these API operations."""
    if not operation_ids or not all(isinstance(item, str) and item for item in operation_ids):
        raise ValueError("operation() needs one or more non-empty operationIds")

    def decorate(fn: F) -> F:
        name = f"{fn.__module__}.{fn.__qualname__}"
        for operation_id in operation_ids:
            _OPERATIONS.setdefault(operation_id, set()).add(name)
        setattr(fn, ATTRIBUTE, tuple(operation_ids))
        return fn

    return decorate


def registered() -> dict[str, tuple[str, ...]]:
    """operationId -> sorted callbacks that call it, for the modules imported so far."""
    return {operation_id: tuple(sorted(names)) for operation_id, names in sorted(_OPERATIONS.items())}


def load_all_commands() -> None:
    """Import every command module listed in `cli.COMMANDS`, so their declarations are registered."""
    from dynfarm.cli import COMMANDS

    for target in COMMANDS.values():
        importlib.import_module(target.partition(":")[0])
