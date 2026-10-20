"""`dynfarm auth`: log in to farms, see which token each one uses, print it for a script, log out, pick the default.

The rules these commands keep, and why:

- **A token is checked before it is kept.** `login` calls `GET /whoami` with it first; a token the farm refuses
  (`token_invalid`, `token_expired`, `farm-api.md` §3.1) is never stored, and the command exits 4 like any other
  authentication failure. Nothing half-works later because of a typo now.
- **A token is never printed**, by any command but `auth token`, whose whole job is to hand it to a script
  (`curl -H "Authorization: Bearer $(dynfarm auth token)"`). `status` names where the token came from (`keyring`,
  `file`, `env:DYNFARM_TOKEN`), never any part of it: even a masked prefix narrows a guess.
- **A token from the environment is `DYNFARM_TOKEN`**, the variable that answers for any farm. No variable of
  another tool is read: every environment variable of the tool is `DYNFARM_*`.
- **Expired is not invalid.** `status` says "expired — get a new one" for `token_expired` and "invalid" for
  `token_invalid`, because the first is routine (renew) and the second means stop.
- **There is no default farm**: `login` takes `--hostname`, `DYNFARM_HOST`, or asks; the first
  farm logged in to becomes the default, `switch` changes it.
- **Logging out does not revoke**: the API has no endpoint for it. The token keeps working until it expires; the
  message says so, so nobody believes a leaked token is dead because they logged out.

Messages for the person (logged in, logged out, warnings, prompts) go to stderr; stdout carries only `status`'s report
and `token`'s token, so both can be piped.
"""

from __future__ import annotations

import sys
from typing import Any

import click

from dynfarm import _brand, commands
from dynfarm.config import normalize_host, resolve_host
from dynfarm.context import AppContext
from dynfarm.errors import AuthRequired, ConfigError, DynfarmError, FarmError, handle
from dynfarm.registry import operation
from dynfarm.tokens import TokenStore, env_token

HOSTNAME_HELP = "The farm's URL, e.g. https://farm.example.com."

#: `auth status` states, and what each tells the person.
OK, EXPIRED, INVALID, UNREACHABLE, FAILED, LOGGED_OUT = "ok", "expired", "invalid", "unreachable", "error", "logged_out"


@click.group()
def auth() -> None:
    """Log in to farms and manage their tokens."""


# -- login ---------------------------------------------------------------------------------------------------------


@auth.command()
@click.option("--hostname", metavar="URL", help=HOSTNAME_HELP + " Asked for when not given.")
@click.option("--with-token", is_flag=True, help="Read the token from standard input (one line).")
@click.option(
    "--insecure-storage",
    is_flag=True,
    help="Keep the token in plain text in the config file (mode 0600) instead of the system keyring.",
)
@operation("whoami")
@handle
def login(hostname: str | None, with_token: bool, insecure_storage: bool) -> None:
    """Log in to a farm with a token.

    The token is checked against the farm before it is saved. Without --with-token it is asked for with a hidden
    prompt; `echo "$TOKEN" | dynfarm auth login --hostname URL --with-token` is the way for scripts.
    """
    store = TokenStore()
    host = _login_host(hostname, store)
    token = _read_token(with_token)
    try:
        me = _whoami(host, token)
    except FarmError as exc:
        for line in exc.render():
            click.echo(line, err=True)
        click.echo("the token was not saved", err=True)
        raise click.exceptions.Exit(exc.exit_code) from None
    saved = store.remember(host, token, insecure=insecure_storage)
    click.echo(f"Logged in to {host} as {me['subject']} ({me['tenant']})", err=True)
    where = "the system keyring" if saved.where == "keyring" else f"{saved.path} (plain text, readable only by you)"
    click.echo(f"Token stored in {where}", err=True)
    if saved.made_default:
        click.echo(f"{host} is now the default farm", err=True)
    shadow = env_token(store.environ)
    if shadow is not None:
        name = shadow.source.removeprefix("env:")
        click.echo(f"warning: {name} is set and takes precedence over the stored token", err=True)


def _login_host(flag: str | None, store: TokenStore) -> str:
    """`--hostname`, else the environment's farm, else a prompt (no built-in default)."""
    for label, value in (
        ("--hostname", flag),
        (_brand.env("HOST"), store.environ.get(_brand.env("HOST"))),
    ):
        if value and value.strip():
            return _normalized(label, value)
    if not commands.is_interactive():
        raise ConfigError("which farm? pass --hostname URL")
    default = store.default_host()
    answer = click.prompt("Farm URL", default=default, err=True)
    return _normalized("farm URL", answer)


def _read_token(with_token: bool) -> str:
    if with_token:
        token = sys.stdin.readline().strip()
        if not token:
            raise ConfigError("--with-token: standard input had no token")
        return token
    if not commands.is_interactive():
        raise ConfigError("not a terminal, so the token cannot be asked for: pipe it in with --with-token")
    token = click.prompt("Paste your token", hide_input=True, err=True).strip()
    if not token:
        raise ConfigError("no token given")
    return token


# -- status --------------------------------------------------------------------------------------------------------


@auth.command()
@click.option("--hostname", metavar="URL", help=HOSTNAME_HELP + " Default: every farm logged in to.")
@click.option("--json", "json_flag", is_flag=True, help="Print the report as JSON.")
@operation("whoami")
@handle
def status(hostname: str | None, json_flag: bool) -> None:
    """Show, for each farm, whose token is in use, where it comes from, and whether the farm accepts it.

    Exits 1 when any farm's token is missing, expired, invalid or cannot be checked. Never prints the token.
    """
    store = TokenStore()
    default = store.default_host()
    if hostname:
        hosts = [_normalized("--hostname", hostname)]
    else:
        hosts = store.hosts()
        value = store.environ.get(_brand.env("HOST"), "")
        if value.strip():
            env_host = _normalized(_brand.env("HOST"), value)
            if env_host not in hosts:
                hosts.append(env_host)
    if not hosts:
        raise AuthRequired(f"not logged in to any farm — run `{_brand.NAME} auth login`")
    report = [_check(store, host, host == default) for host in hosts]
    if json_flag:
        import json

        click.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for row in report:
            for line in _describe(row):
                click.echo(line)
    if any(row["state"] != OK for row in report):
        raise click.exceptions.Exit(1)


def _check(store: TokenStore, host: str, is_default: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"host": host, "default": is_default, "state": LOGGED_OUT, "source": None}
    try:
        resolved = store.resolve(host)
    except ConfigError as exc:
        return {**row, "state": FAILED, "error": {"code": "config_error", "message": exc.message}}
    if resolved is None:
        return row
    row["source"] = resolved.source
    try:
        me = _whoami(host, resolved.token)
    except FarmError as exc:
        state = {"token_expired": EXPIRED, "token_invalid": INVALID, "connection_failed": UNREACHABLE}.get(
            exc.code, FAILED
        )
        return {**row, "state": state, "error": {"code": exc.code, "message": exc.message}}
    except ConfigError as exc:  # a stored token that cannot be sent in a header
        return {**row, "state": INVALID, "error": {"code": "token_malformed", "message": exc.message}}
    return {
        **row,
        "state": OK,
        "subject": me.get("subject"),
        "tenant": me.get("tenant"),
        "scopes": me.get("scopes", []),
        "is_admin": me.get("is_admin"),
        "expires_at": me.get("expires_at"),
    }


def _describe(row: dict[str, Any]) -> list[str]:
    host = row["host"]
    lines = [host + (" (default)" if row["default"] else "")]
    state = row["state"]
    if state == LOGGED_OUT:
        return [*lines, f"  not logged in — run `{_brand.NAME} auth login --hostname {host}`"]
    if row["source"]:
        lines.append(f"  token from: {row['source']}")
    if state == OK:
        return [
            *lines,
            f"  logged in as {row['subject']} ({row['tenant']})" + (", tenant admin" if row["is_admin"] else ""),
            f"  scopes: {', '.join(row['scopes']) or '(none)'}",
            f"  expires: {row['expires_at']}",
        ]
    error = row["error"]
    relogin = f"`{_brand.NAME} auth login --hostname {host}`"
    if state == EXPIRED:
        return [*lines, f"  expired — get a new token and run {relogin}"]
    if state == INVALID:
        return [*lines, f"  invalid — the farm rejects this token; log in again with a new one: {relogin}"]
    if state == UNREACHABLE:
        return [*lines, f"  unreachable — {error['message']}"]
    return [*lines, f"  could not check — {error['message']} ({error['code']})"]


# -- token ---------------------------------------------------------------------------------------------------------


@auth.command()
@click.option("--hostname", metavar="URL", help=HOSTNAME_HELP + " Default: the default farm.")
@handle
def token(hostname: str | None) -> None:
    """Print the token for a farm, for scripts.

    This is the only command that prints a token: `curl -H "Authorization: Bearer $(dynfarm auth token)" ...`.
    Do not run it where its output is recorded (a CI log, a shared screen).
    """
    host = resolve_host(hostname)
    resolved = TokenStore().resolve(host)
    if resolved is None:
        raise _not_logged_in(host)
    click.echo(resolved.token)


# -- logout --------------------------------------------------------------------------------------------------------


@auth.command()
@click.option("--hostname", metavar="URL", help=HOSTNAME_HELP + " Default: the default farm.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@handle
def logout(hostname: str | None, yes: bool) -> None:
    """Remove a farm's stored token and forget the farm.

    The token is not revoked: the farm has no endpoint for that, so it keeps working until it expires. If it leaked,
    ask the farm's administrator to revoke it.
    """
    store = TokenStore()
    host = resolve_host(hostname)
    if host not in store.hosts() and not store.has_stored_token(host):
        raise DynfarmError(f"not logged in to {host}")
    commands.confirm(f"Log out of {host}?", yes=yes)
    removed, default = store.forget(host)
    where = f" (token removed from: {', '.join(removed)})" if removed else ""
    click.echo(f"Logged out of {host}{where}", err=True)
    click.echo("The token was not revoked: it works until it expires. If it leaked, ask the farm's admin.", err=True)
    if default:
        click.echo(f"The default farm is {default}", err=True)
    shadow = env_token(store.environ)
    if shadow is not None:
        name = shadow.source.removeprefix("env:")
        click.echo(f"warning: {name} is still set in this environment", err=True)


# -- switch --------------------------------------------------------------------------------------------------------


@auth.command()
@click.option("--hostname", metavar="URL", required=True, help=HOSTNAME_HELP + " Must be logged in to.")
@handle
def switch(hostname: str) -> None:
    """Make another farm the default one."""
    store = TokenStore()
    host = _normalized("--hostname", hostname)
    if host not in store.hosts():
        raise ConfigError(f"not logged in to {host} — run `{_brand.NAME} auth login --hostname {host}` first")
    store.set_default(host)
    click.echo(f"{host} is now the default farm", err=True)
    if store.environ.get(_brand.env("HOST"), "").strip():
        click.echo(f"warning: {_brand.env('HOST')} is set and overrides the default farm", err=True)


# -- shared --------------------------------------------------------------------------------------------------------


def _app() -> AppContext:
    ctx = click.get_current_context(silent=True)
    found = ctx.find_object(AppContext) if ctx is not None else None
    return found or AppContext()


def _whoami(host: str, token: str) -> dict[str, Any]:
    """`GET /whoami` with this token; a `FarmError` when the farm refuses it or cannot be reached."""
    from dynfarm.http import FarmClient

    app = _app()
    with FarmClient(host, token, transport=app.transport, debug=app.debug or None, sleep=app.sleep) as client:
        me = client.request("GET", "/whoami", operation_id="whoami")
    if not (isinstance(me, dict) and isinstance(me.get("subject"), str) and isinstance(me.get("tenant"), str)):
        raise FarmError(200, "response_invalid", f"{host} did not answer /whoami like an Agent Farm")
    return me


def _normalized(label: str, value: str) -> str:
    try:
        return normalize_host(value)
    except ConfigError as exc:
        raise ConfigError(f"{label}: {exc.message}") from None


def _not_logged_in(host: str) -> AuthRequired:
    return AuthRequired(
        f"no token for {host} — run `{_brand.NAME} auth login --hostname {host}`, or set {_brand.env('TOKEN')}"
    )
