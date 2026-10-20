"""How a command prints what the farm answered: JSON for scripts, a table for people, tab-separated rows for pipes.

One function, `emit`, so every command behaves the same (the `gh` way):

- `--json`: the API's JSON verbatim, indented, keys in the order the farm sent them. Nothing is renamed or dropped, so
  a script can rely on the API contract (`docs/farm-api.md`) rather than on this tool's choices.
- a terminal: an aligned table with headers, dates relative ("3 hours ago"), bold headers unless `NO_COLOR` is set.
- a pipe or a file: tab-separated rows, no headers, ISO dates, no colour: `cut`, `awk` and `while read` just work.

Text that comes from the API or from other people (a task's prompt, an agent's description) is cleaned of control
characters before it is printed in a table, so an escape sequence in a prompt cannot rewrite the person's terminal.
JSON output needs no such care: JSON escapes control characters itself.

Which columns a resource shows is each command's business (a `Column` list); only the mechanism is here.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any

import click


@dataclass(frozen=True)
class Column:
    """One table column: its header, where its value is (a dotted key such as `usage.cost_eur`, or a function), and
    whether it is a `"text"` or a `"date"` (shown relative on a terminal, as sent otherwise)."""

    header: str
    key: str | Callable[[Any], Any]
    kind: str = "text"


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_KEEPING_LAYOUT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def use_color(stream: IO[str]) -> bool:
    """Colour only on a terminal, and never when `NO_COLOR` is set to anything non-empty (no-color.org)."""
    if os.environ.get("NO_COLOR"):
        return False
    return _is_tty(stream) and os.environ.get("TERM") != "dumb"


def emit(
    data: Any,
    *,
    json_flag: bool = False,
    table: Sequence[Column] | None = None,
    rows_key: str | None = "items",
    stream: IO[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Print `data`, the parsed body of an API answer.

    The rows of the table are `data[rows_key]` when `data` is an object that has it (`{"items": [...]}`), `data` when it
    is a list, and `[data]` for a single object, so a `view` command is a one-row table. With no `table`, or with
    `json_flag`, the answer is printed as JSON.
    """
    stream = stream or sys.stdout
    if json_flag or table is None:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False), file=stream)
        return
    rows = _rows(data, rows_key)
    tty = _is_tty(stream)
    now = now or datetime.now(UTC)
    cells = [[_cell(row, column, tty, now) for column in table] for row in rows]
    if not tty:
        for line in cells:
            click.echo("\t".join(line), file=stream)
        return
    if not cells:
        click.echo("no results", err=True)
        return
    headers = [column.header.upper() for column in table]
    widths = [max(_width(text) for text in (header, *(line[i] for line in cells))) for i, header in enumerate(headers)]
    color = use_color(stream)
    head = [click.style(h, bold=True) if color else h for h in headers]
    click.echo(_join(head, [_width(h) for h in headers], widths), file=stream)
    for line in cells:
        click.echo(_join(line, [_width(c) for c in line], widths), file=stream)


def clean_text(text: str, *, multiline: bool = False) -> str:
    """`text` from the farm or from other people, made safe to print: control characters become spaces.

    With `multiline` the line breaks and tabs of a prompt or a log line stay, so it keeps its shape; the rest (escape
    sequences included) cannot rewrite the person's terminal.
    """
    return (_CONTROL_KEEPING_LAYOUT if multiline else _CONTROL).sub(" ", text)


def relative_time(when: datetime, now: datetime) -> str:
    """`3 hours ago`, `in 2 days`, or the date when it is more than a month away. Naive datetimes are UTC."""
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    seconds = (now - when).total_seconds()
    future = seconds < 0
    span = abs(seconds)
    if span < 60:
        return "in less than a minute" if future else "less than a minute ago"
    for unit, size, limit in (("minute", 60, 3600), ("hour", 3600, 86400), ("day", 86400, 30 * 86400)):
        if span < limit:
            count = int(span // size)
            text = f"{count} {unit}{'' if count == 1 else 's'}"
            return f"in {text}" if future else f"{text} ago"
    return when.strftime("%Y-%m-%d")


# -- internals ----------------------------------------------------------------------------------------------------


def _is_tty(stream: IO[str]) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _rows(data: Any, rows_key: str | None) -> list[Any]:
    if isinstance(data, dict) and rows_key and isinstance(data.get(rows_key), list):
        return data[rows_key]
    if isinstance(data, list):
        return data
    return [data]


def _lookup(row: Any, key: str | Callable[[Any], Any]) -> Any:
    if callable(key):
        return key(row)
    value = row
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _cell(row: Any, column: Column, tty: bool, now: datetime) -> str:
    value = _lookup(row, column.key)
    if value is None:
        return "-" if tty else ""
    if column.kind == "date" and isinstance(value, str):
        if tty:
            try:
                return relative_time(datetime.fromisoformat(value), now)
            except ValueError:
                pass
    elif isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _CONTROL.sub(" ", str(value))


def _width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _join(cells: list[str], widths_of_cells: list[int], widths: list[int]) -> str:
    padded = [cell + " " * (width - own) for cell, own, width in zip(cells, widths_of_cells, widths, strict=True)]
    return "  ".join(padded).rstrip()
