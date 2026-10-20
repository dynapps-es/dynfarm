"""`dynfarm api`: any call to the farm's API, for scripts and for what no curated command covers yet.

It is modelled on `gh api`, so what a person knows from it carries over, and it keeps the promises the rest of the tool
makes:

- **The token cannot be redirected.** The path is relative to the API (`/farm/api/v1`) and is joined to the *selected
  farm's* URL: a full URL, a `..` segment or a `-H Authorization:` header is refused (exit 2), so this command cannot be
  turned into "send my token somewhere else". The `Authorization` header always comes from the token store.
- **Fields go where the method says**: into the query string on `GET` (and whenever `--input` carries the body), into
  a JSON body otherwise. As in `gh api`, giving fields without `-X` means `POST`; `-X GET` makes them a query, and
  `--paginate` implies `GET`. `-f` fields are strings; `-F` fields are typed (`true`, `false`, `null`, integers,
  `@file` or `@-` for text read from a file or standard input). Nested bodies come through `--input`.
- **The output is always JSON**, verbatim as the farm sent it (this command is for scripts; the curated commands print
  tables). Errors are the same as everywhere: the farm's `error.code`, a stable exit code, retries only on `GET`.
- **It never asks for confirmation.** `-X DELETE` is a raw call and a script must be able to make it; the curated
  `... delete` commands are where the question is asked.
- **A secret passed with `-f` or `-F` is in the shell's history or the process list.** The credential value belongs in
  `dynfarm credential set`, which reads it from a hidden prompt or a file; `api` is not the road for it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote

import click

from dynfarm.errors import ConfigError, FarmError
from dynfarm.http import API_PREFIX, FarmClient, needs_client
from dynfarm.output import emit

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_INTEGER = re.compile(r"-?\d+")


@click.command()
@click.argument("path")
@click.option(
    "-X",
    "--method",
    type=click.Choice(METHODS, case_sensitive=False),
    help="The HTTP method. Default: GET, or POST when fields or --input are given (add -X GET to send them as "
    "a query).",
)
@click.option(
    "-f",
    "--raw-field",
    "raw_fields",
    multiple=True,
    metavar="KEY=VALUE",
    help="Add a string field (repeatable).",
)
@click.option(
    "-F",
    "--field",
    "typed_fields",
    multiple=True,
    metavar="KEY=VALUE",
    help="Add a typed field: true, false, null and integers are converted; @FILE (or @-) reads text from a file "
    "(or standard input). Repeatable.",
)
@click.option(
    "--input",
    "input_file",
    metavar="FILE",
    help="Send this file ('-' for standard input) as the JSON body; -f and -F fields then go to the query string.",
)
@click.option(
    "-H", "--header", "headers", multiple=True, metavar="'NAME: VALUE'", help="Add a request header (repeatable)."
)
@click.option("--paginate", is_flag=True, help="Follow next_cursor and merge every page's items (GET only).")
@click.option("-i", "--include", is_flag=True, help="Print the status line and response headers before the body.")
@needs_client
def api(
    client: FarmClient,
    path: str,
    method: str | None,
    raw_fields: tuple[str, ...],
    typed_fields: tuple[str, ...],
    input_file: str | None,
    headers: tuple[str, ...],
    paginate: bool,
    include: bool,
) -> None:
    """Call any endpoint of the farm's API and print the JSON answer.

    PATH is relative to /farm/api/v1 (a leading /farm/api/v1 is accepted too).

    \b
      dynfarm api whoami
      dynfarm api -X GET tasks -f state=running -F limit=5
      dynfarm api tasks --paginate
      dynfarm api -X POST schedules/SCHEDULE_ID/pause
      echo '{"agent_id": "code-reviewer", "prompt": "..."}' | dynfarm api tasks --input -
    """
    route, query = _route(path)
    fields = _fields(raw_fields, typed_fields, uses_stdin=input_file == "-")
    body = _body(input_file)
    # Like `gh api`: fields or a body mean POST unless a method is given; a paginated call is a listing, so GET.
    http_method = (method or ("POST" if (fields or body is not None) and not paginate else "GET")).upper()
    if paginate and http_method != "GET":
        raise ConfigError("--paginate follows a listing, so it works with GET only")
    request_headers = _headers(headers)
    # Fields are the query on GET and next to an --input body; otherwise they are the JSON body.
    if http_method == "GET" or body is not None:
        query.update(fields)
        json_body = body
    else:
        json_body = fields or None
    try:
        data = _fetch(client, http_method, route, query, json_body, request_headers, paginate)
    except FarmError:
        if include:
            _print_reply(client, err=True)
        raise
    if include:
        _print_reply(client, err=False)
    if data is not None:
        emit(data, json_flag=True)


# -- the request, from the arguments -------------------------------------------------------------------------------


def _route(path: str) -> tuple[str, dict[str, Any]]:
    """`/farm/api/v1/tasks?state=running` -> (`/tasks`, `{"state": "running"}`); refuses anything but a path."""
    text = path.strip()
    if "://" in text or text.startswith("//"):
        raise ConfigError("the path is relative to the farm's API (e.g. tasks); a full URL is not accepted")
    if "#" in text:
        raise ConfigError("the path must not carry a fragment")
    text, _, raw_query = text.partition("?")
    segments = [part for part in text.split("/") if part]
    prefix = [part for part in API_PREFIX.split("/") if part]
    if segments[: len(prefix)] == prefix:
        segments = segments[len(prefix) :]
    if not segments:
        raise ConfigError("which endpoint? e.g. `whoami` or `tasks`")
    if any(unquote(part) in (".", "..") for part in segments):
        raise ConfigError("the path must not contain '.' or '..' segments")
    return "/" + "/".join(segments), dict(parse_qsl(raw_query, keep_blank_values=True))


def _fields(raw_fields: tuple[str, ...], typed_fields: tuple[str, ...], *, uses_stdin: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for item in raw_fields:
        key, value = _split("-f", item)
        fields[key] = value
    for item in typed_fields:
        key, value = _split("-F", item)
        fields[key] = _typed(value, uses_stdin=uses_stdin)
    return fields


def _split(flag: str, item: str) -> tuple[str, str]:
    key, separator, value = item.partition("=")
    if not separator or not key:
        # The item is not echoed: a value pasted without its key could be a secret.
        raise ConfigError(f"{flag}: expected KEY=VALUE")
    return key, value


def _typed(value: str, *, uses_stdin: bool) -> Any:
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if _INTEGER.fullmatch(value):
        return int(value)
    if value.startswith("@"):
        source = value[1:]
        if source == "-":
            if uses_stdin:
                raise ConfigError("-F @-: standard input is already the body (--input -)")
            return sys.stdin.read()
        return _read_text("-F", source)
    return value


def _read_text(flag: str, source: str) -> str:
    try:
        # `utf-8-sig`: a file saved by a Windows editor starts with a byte-order mark that is not part of the text.
        return Path(source).expanduser().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{flag}: cannot read {source}: {exc.__class__.__name__}") from None


def _body(input_file: str | None) -> Any:
    if input_file is None:
        return None
    text = sys.stdin.read() if input_file == "-" else _read_text("--input", input_file)
    try:
        return json.loads(text)
    except ValueError as exc:
        # Position only: the text may be a secret, and a JSON error message would quote nothing, but be explicit.
        line, column = getattr(exc, "lineno", "?"), getattr(exc, "colno", "?")
        raise ConfigError(f"--input: not valid JSON (line {line}, column {column})") from None


def _headers(items: tuple[str, ...]) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition(":")
        name, value = name.strip(), value.strip()
        if not separator or not _HEADER_NAME.fullmatch(name):
            raise ConfigError("-H: expected 'Name: value'")
        if name.lower() == "authorization":
            raise ConfigError("-H: the Authorization header comes from the stored token and cannot be overridden")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ConfigError("-H: a header value cannot contain control characters")
        headers[name] = value
    return headers or None


# -- the call and its output ---------------------------------------------------------------------------------------


def _fetch(
    client: FarmClient,
    method: str,
    route: str,
    query: dict[str, Any],
    body: Any,
    headers: dict[str, str] | None,
    paginate: bool,
) -> Any:
    if not paginate:
        return client.request(method, route, params=query or None, json=body, headers=headers)
    items: list[Any] = []
    seen: set[str] = set()
    while True:
        page = client.request("GET", route, params=query or None, headers=headers)
        if not (isinstance(page, dict) and isinstance(page.get("items"), list)):
            # Not a listing: there is nothing to merge, so the answer is shown as it came.
            return page if not items else {"items": items}
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            return {**page, "items": items}
        if cursor in seen:
            raise FarmError(200, "pagination_loop", f"the farm sent the cursor {cursor!r} twice; stopping")
        seen.add(cursor)
        query = {**query, "cursor": cursor}


def _print_reply(client: FarmClient, *, err: bool) -> None:
    """The status line and headers of the last answer, then a blank line, as `gh api --include` does."""
    reply = client.last_reply
    if reply is None:  # the farm could not be reached: there is no answer to show
        return
    click.echo(f"HTTP {reply.status} {reply.reason}".rstrip(), err=err)
    for name, value in reply.headers:
        click.echo(f"{name}: {value}", err=err)
    click.echo(err=err)
