"""The one way every command talks to the farm: `FarmClient`, plus `@needs_client` to give a command one.

What this module guarantees, and why each point exists:

- **The token goes only where it may.** The client refuses (exit 2) a plain `http://` host that is not loopback
  and a token with characters that cannot sit in a header. The token is in the `Authorization` header and
  nowhere else: not in the URL, not in the debug line, and if the farm's own text ever echoed it the client masks it.
- **Errors are the farm's, branched on `error.code`** (`farm-api.md` §15): the envelope becomes a `FarmError`; FastAPI's
  `{"detail": [...]}` 422 (a malformed query or path value) becomes `request_invalid`; a reply that is not an
  envelope at all (a proxy's HTML, a redirect) becomes a generic error naming only the HTTP status.
- **Retries are conservative**: only `GET`, only when the error says it is retryable, at most 3 attempts,
  waiting the farm's `Retry-After` (capped at 30 s). A `POST`/`PATCH`/`DELETE` never repeats by itself, because
  repeating a launch could launch twice; the error tells the person it can be repeated.
- **Timeouts**: 10 s to connect, 60 s to read, so a `wait=30` on a task fits.
- **`--debug` prints method, path, status and milliseconds to stderr, never a header or a body**: a bodies-and-headers
  trace would put tokens and credential values in terminal scrollback and CI logs.

Token lookup is a callable injected through `AppContext.token_provider` (tests inject one); without one it is
`dynfarm.tokens.token_for`: the environment, then the keyring, then the config file.
"""

from __future__ import annotations

import email.utils
import functools
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import click
import httpx

from dynfarm import _brand
from dynfarm.config import normalize_host, resolve_host
from dynfarm.context import AppContext
from dynfarm.errors import AuthRequired, ConfigError, FarmError, handle

API_PREFIX = "/farm/api/v1"

#: Attempts for an idempotent call (the first plus two retries), and the longest we ever wait on a `Retry-After`.
MAX_ATTEMPTS = 3
RETRY_AFTER_CAP = 30.0
#: Wait before attempt 2, 3 when the farm sent no `Retry-After`.
BACKOFF = (0.5, 1.0)

TIMEOUT = httpx.Timeout(60.0, connect=10.0, pool=10.0)

_TOKEN_OK = re.compile(r"[\x21-\x7e]+")
_FALSEY = {"", "0", "false", "no", "off"}


def segment(value: str) -> str:
    """Escape one path segment (an id, a slug, a credential name): `a/b` or `..` must not reach another route."""
    return quote(value, safe="")


def default_token_provider(host: str) -> str | None:
    """The token store's answer for `host` (`dynfarm.tokens`), imported only when a command needs a token."""
    from dynfarm.tokens import token_for

    return token_for(host)


def debug_from_env() -> bool:
    return os.environ.get(_brand.env("DEBUG"), "").strip().lower() not in _FALSEY


def parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` as seconds: a number of seconds or an HTTP date. Anything else is ignored (None)."""
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d{1,9}", text):
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True)
class Reply:
    """What came back besides the body: the status line and the headers (the token masked, should the farm echo it)."""

    status: int
    reason: str
    headers: list[tuple[str, str]]


class FarmClient:
    """A client for one farm and one token. Build it with `with FarmClient(host, token) as client:`."""

    def __init__(
        self,
        host: str,
        token: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        debug: bool | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.host = normalize_host(host)
        if token is not None and not _TOKEN_OK.fullmatch(token):
            raise ConfigError(
                "the token has characters that cannot be sent in an HTTP header (spaces or control codes)"
            )
        self._token = token or None
        self.debug = debug_from_env() if debug is None else debug
        self._sleep = sleep or time.sleep
        #: Status and headers (token masked) of the last reply, error or not, for `dynfarm api --include`.
        self.last_reply: Reply | None = None
        headers = {"User-Agent": _brand.user_agent(), "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._http = httpx.Client(transport=transport, headers=headers, timeout=TIMEOUT)

    def __enter__(self) -> FarmClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- requests ---------------------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        operation_id: str | None = None,
    ) -> Any:
        """One API call; returns the parsed JSON body (`None` for an empty one) or raises `FarmError`.

        `path` is relative to `/farm/api/v1` and starts with `/`; escape variable parts with `segment()`.
        `headers` are extra request headers (only `dynfarm api -H` sets them; the caller has already refused
        `Authorization`). `operation_id` only labels the debug line.
        """
        method = method.upper()
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        url = self.host + API_PREFIX + path
        attempts = MAX_ATTEMPTS if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                return self._once(method, url, API_PREFIX + path, params, json, headers, operation_id)
            except FarmError as exc:
                if attempt == attempts or not exc.retryable:
                    raise
                delay = min(exc.retry_after, RETRY_AFTER_CAP) if exc.retry_after is not None else BACKOFF[attempt - 1]
                self._log(
                    f"{method} {API_PREFIX + path} -> {exc.code}; retrying in {delay:g} s ({attempt + 1}/{attempts})"
                )
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _once(
        self,
        method: str,
        url: str,
        shown_path: str,
        params: dict[str, Any] | None,
        body: Any,
        headers: dict[str, str] | None,
        operation_id: str | None,
    ) -> Any:
        label = f"{method} {shown_path}" + (f" ({operation_id})" if operation_id else "")
        started = time.monotonic()
        try:
            response = self._http.request(method, url, params=_query(params), json=body, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            self._log(f"{label} -> connection failed in {_ms(started)} ms")
            raise self._network_error(method, exc) from None
        self._log(f"{label} -> {response.status_code} in {_ms(started)} ms")
        self.last_reply = Reply(
            response.status_code,
            response.reason_phrase,
            [(name, self._mask(value)) for name, value in response.headers.items()],
        )
        if 200 <= response.status_code < 300:
            return self._success(response)
        raise self._error(response)

    def pages(
        self, path: str, *, params: dict[str, Any] | None = None, operation_id: str | None = None
    ) -> dict[str, Any]:
        """Every page of a listing, merged: `{"items": [...], "next_cursor": None}`, following `next_cursor`.

        `params` may carry a first `cursor` to start from. A farm that sends the same cursor twice would make this loop
        forever, so it is stopped with `pagination_loop`.
        """
        items: list[Any] = []
        seen: set[str] = set()
        query = dict(params or {})
        while True:
            page = self.request("GET", path, params=query, operation_id=operation_id)
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                return {**page, "items": items, "next_cursor": None}
            if cursor in seen:
                raise FarmError(200, "pagination_loop", f"the farm sent the cursor {cursor!r} twice; stopping")
            seen.add(cursor)
            query["cursor"] = cursor

    def pause(self, seconds: float) -> None:
        """Wait, through the same seam the retries use, so tests record the pause instead of sleeping it."""
        self._sleep(seconds)

    # -- replies ----------------------------------------------------------------------------------------------

    def _success(self, response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise FarmError(
                response.status_code,
                "response_invalid",
                f"{self.host} answered {response.status_code} but not with JSON — is it an Agent Farm?",
            ) from None

    def _error(self, response: httpx.Response) -> FarmError:
        status = response.status_code
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        try:
            body = response.json()
        except ValueError:
            body = None
        envelope = body.get("error") if isinstance(body, dict) else None
        if isinstance(envelope, dict):
            details = envelope.get("details")
            return FarmError(
                status,
                str(envelope.get("code") or f"http_{status}"),
                self._mask(str(envelope.get("message") or f"the farm answered HTTP {status}")),
                retryable=envelope.get("retryable") is True,
                details=self._mask(details) if isinstance(details, dict) else None,
                retry_after=retry_after,
                correlation_id=envelope.get("correlation_id"),
            )
        if status == 422 and isinstance(body, dict) and "detail" in body:
            return _validation_error(body["detail"])
        if status == 401:
            expired = "expired" in response.headers.get("WWW-Authenticate", "")
            code = "token_expired" if expired else "token_invalid"
            return FarmError(status, code, "the farm did not accept the credentials", retry_after=retry_after)
        reason = httpx.codes.get_reason_phrase(status)
        return FarmError(
            status, "http_error", f"the farm answered HTTP {status} {reason}".rstrip(), retry_after=retry_after
        )

    def _network_error(self, method: str, exc: Exception) -> FarmError:
        """A reply never arrived. Safe to repeat when the request cannot have been processed."""
        never_sent = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
        reason = "timed out" if isinstance(exc, httpx.TimeoutException) else (str(exc) or exc.__class__.__name__)
        message = f"could not reach {self.host}: {reason}"
        if not (never_sent or method == "GET"):
            message += " — the farm may have received the request; check before repeating it"
        return FarmError(0, "connection_failed", self._mask(message), retryable=never_sent or method == "GET")

    # -- never leak --------------------------------------------------------------------------------------------

    def _mask(self, value: Any) -> Any:
        """Replace the token wherever the farm's own text happens to contain it."""
        if not self._token:
            return value
        if isinstance(value, str):
            return value.replace(self._token, "[redacted]")
        if isinstance(value, dict):
            return {key: self._mask(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._mask(item) for item in value]
        return value

    def _log(self, line: str) -> None:
        if self.debug:
            click.echo(f"[debug] {line}", err=True)


def _validation_error(detail: Any) -> FarmError:
    """FastAPI's own 422 (`{"detail": [{"loc", "msg", "type"}]}`), without the echoed `input`, which may be a secret."""
    problems: list[dict[str, Any]] = []
    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, dict):
                problems.append(
                    {
                        "loc": [str(part) for part in item.get("loc", [])],
                        "msg": str(item.get("msg", "")),
                        "type": item.get("type"),
                    }
                )
        text = "; ".join(f"{'.'.join(p['loc'])}: {p['msg']}" if p["loc"] else p["msg"] for p in problems[:5])
    else:
        text = str(detail)
    return FarmError(
        422,
        "request_invalid",
        f"the farm rejected the request: {text or 'invalid input'}",
        details={"errors": problems},
    )


def _query(params: dict[str, Any] | None) -> dict[str, str] | None:
    """Drop unset options and spell booleans the way FastAPI reads them."""
    if not params:
        return None
    out: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        out[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return out


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def needs_client(fn: Callable[..., Any] | None = None, *, require_token: bool = True) -> Any:
    """Decorator for a command callback: adds `--hostname`, builds a `FarmClient` and passes it as the first argument.

        @click.command()
        @needs_client
        def whoami(client): ...

    The host comes from `resolve_host(--hostname)`, the token from the context's `token_provider`. With no token the
    command stops with exit 4 before any request, unless `require_token=False` (a command that works anonymously).
    Errors raised inside become a message and an exit code through `handle`.
    """

    def decorate(callback: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(callback)
        def wrapper(*args: Any, hostname: str | None = None, **kwargs: Any) -> Any:
            app = click.get_current_context().ensure_object(AppContext)
            host = resolve_host(hostname)
            token = (app.token_provider or default_token_provider)(host)
            if token is None and require_token:
                raise AuthRequired(
                    f"no token for {host} — run `{_brand.NAME} auth login`, or set {_brand.env('TOKEN')}"
                )
            with FarmClient(host, token, transport=app.transport, debug=app.debug or None, sleep=app.sleep) as client:
                return callback(client, *args, **kwargs)

        wrapper = handle(wrapper)
        return click.option(
            "--hostname", metavar="URL", help="The farm to talk to (default: the one `auth login` set as default)."
        )(wrapper)

    return decorate(fn) if fn is not None else decorate
