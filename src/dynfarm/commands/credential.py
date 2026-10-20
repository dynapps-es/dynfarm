"""`dynfarm credential`: hand a secret to the farm, see what it keeps about it, and delete it.

    credential list         GET    /credentials           credential set NAME     POST   /credentials
    credential view NAME    GET    /credentials/{name}    credential delete NAME  DELETE /credentials/{name}

The farm's credential channel is write-only (`farm-api.md` §8): a secret goes in once, by name, and no endpoint ever
gives it back, only its metadata and a `fingerprint` (a prefix of its SHA-256) to tell which one is stored. Tasks and
agents then refer to it by name. This is the one operation the farm's MCP deliberately does not offer: a secret must
not pass through an LLM conversation, so it is done here, from a terminal or a script.

What `credential set` guarantees about the secret, and why:

- **It never comes from the command line.** There is no `--value` option: an argument is in the shell history, in
  `ps` for every user of the machine and in a CI job's log. The value comes from a hidden prompt (asked twice), from
  standard input, from `--value-file FILE` or from `--value-env VARIABLE`. `--value SECRET`, `--value=SECRET` and a
  stray positional argument are usage errors (exit 2) whose message does not repeat what was typed.
- **It goes to one place**: the body of the one `POST /credentials`. It is not printed, not logged (`--debug` never
  shows a body, see `dynfarm.http`), not written to any file, and not repeated: a `POST` is never retried by itself.
  If the farm's error message ever quoted it, the message is masked before it is shown.
- **What is read is what is stored**: one trailing line break (the one `echo` or an editor adds) is removed from
  standard input and from a file, nothing else, so a value is never trimmed into another one. An empty value, or one
  of only whitespace, is refused (exit 2) before anything is sent.
- **The type is checked here** against the farm's list (`CREDENTIAL_TYPES`, the enum of `CredentialCreate` in the
  OpenAPI) before the value is even read, so a typo does not cost the person typing the secret again.

Writing a name you already have **rotates** it (the farm answers `200` and `set` says "Rotated"); a new name answers
`201` ("Saved"). `--scope shared` writes a credential of the whole team, which only a tenant admin may do
(`403 insufficient_scope`); the team's agents refer to it as `shared:<name>`. Someone else's credential is a `404`,
never a `403`. `delete` asks first, like every command that removes something; a task already running keeps the copy
it was given, the next one that names the credential fails.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import click

from dynfarm import _brand, commands
from dynfarm.errors import ConfigError, FarmError
from dynfarm.http import FarmClient, needs_client, segment
from dynfarm.output import Column, clean_text, emit
from dynfarm.registry import operation

JSON_HELP = "Print the farm's answer as JSON (metadata only: the farm never returns a value)."

#: The `type` enum of `CredentialCreate` in the farm's OpenAPI; a test keeps it equal to the pinned document.
CREDENTIAL_TYPES = ("git_token", "s3", "model_gateway_key", "webhook_hmac", "generic", "mcp_url", "mcp_token")

SCOPE_OPTION_HELP = "`personal` (default): your own. `shared`: your team's (written and deleted by a tenant admin)."

#: Options a person may reach for to pass the secret on the command line. They do not exist; the error says why.
_ARGV_VALUE_OPTIONS = ("--value", "--secret", "--password", "--token")

CREDENTIAL_COLUMNS = (
    Column("name", "name"),
    Column("type", "type"),
    Column("scope", "scope"),
    Column("fingerprint", "fingerprint"),
    Column("expires", "expires_at", "date"),
    Column("updated", "updated_at", "date"),
)


def _scope_option(fn: Any) -> Any:
    return click.option("--scope", type=click.Choice(["personal", "shared"]), help=SCOPE_OPTION_HELP)(fn)


@click.group()
def credential() -> None:
    """Hand secrets to the farm (write-only), see their metadata and delete them."""


# -- list ---------------------------------------------------------------------------------------------------------


@credential.command("list")
@_scope_option
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("list_credentials")
@needs_client
def list_(client: FarmClient, scope: str | None, json_flag: bool) -> None:
    """List your credentials (or your team's with --scope shared): name, type, fingerprint, expiry. Never a value."""
    found = client.request("GET", "/credentials", params={"scope": scope}, operation_id="list_credentials")
    emit(found, json_flag=json_flag, table=CREDENTIAL_COLUMNS)


# -- view ---------------------------------------------------------------------------------------------------------


@credential.command()
@click.argument("name")
@_scope_option
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("get_credential")
@needs_client
def view(client: FarmClient, name: str, scope: str | None, json_flag: bool) -> None:
    """Show what the farm keeps about a credential: its type, fingerprint, description, metadata and dates."""
    found = client.request(
        "GET", f"/credentials/{segment(name)}", params={"scope": scope}, operation_id="get_credential"
    )
    if json_flag:
        emit(found, json_flag=True)
        return
    metadata = found.get("metadata") or {}
    rows = [
        ("name", found.get("name")),
        ("scope", found.get("scope")),
        ("type", found.get("type")),
        ("fingerprint", found.get("fingerprint")),
        ("description", found.get("description")),
        ("metadata", ", ".join(f"{key}={value}" for key, value in metadata.items())),
        ("expires", found.get("expires_at")),
        ("created", found.get("created_at")),
        ("updated", found.get("updated_at")),
        ("tenant", found.get("tenant")),
    ]
    shown = [(label, value) for label, value in rows if value not in (None, "")]
    width = max(len(label) for label, _ in shown) + 1
    for label, value in shown:
        click.echo(f"{label + ':':<{width}} {clean_text(str(value))}")


# -- set ----------------------------------------------------------------------------------------------------------


class SecretSafeCommand(click.Command):
    """A command whose usage errors never repeat a stray argument, because that argument may be the secret.

    click answers `set NAME glpat-...` with "Got unexpected extra argument (glpat-...)", which puts the secret on the
    screen and in a CI log a second time. Here the extra arguments are counted, not shown. click's own message for
    an unknown option already names only the option (`--value`, never `--value=...`'s value); for the options a
    person would reach for to pass the secret, it also says how to pass it instead.
    """

    def make_context(self, info_name: str | None, args: list[str], parent: click.Context | None = None, **extra: Any):
        extra.setdefault("allow_extra_args", True)
        return super().make_context(info_name, args, parent=parent, **extra)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            rest = super().parse_args(ctx, args)
        except click.NoSuchOption as exc:
            if exc.option_name in _ARGV_VALUE_OPTIONS:
                raise click.UsageError(
                    f"there is no {exc.option_name} option: a secret on the command line stays in the shell history "
                    "and is visible to other processes. Type it at the hidden prompt, pipe it into standard input, "
                    "or use --value-file FILE or --value-env VARIABLE. If you typed a real secret, rotate it: it is in "
                    "your shell history now.",
                    ctx=ctx,
                ) from None
            raise
        if ctx.args:
            count = len(ctx.args)
            raise click.UsageError(
                f"got {count} unexpected argument{'s' if count > 1 else ''} (not shown, in case it is the secret). "
                "The value is never given on the command line: use the hidden prompt, standard input, "
                "--value-file FILE or --value-env VARIABLE. If it was a real secret, rotate it: it is in your shell "
                "history now.",
                ctx=ctx,
            )
        return rest


@credential.command("set", cls=SecretSafeCommand)
@click.argument("name")
@click.option("--type", "type_", required=True, type=click.Choice(CREDENTIAL_TYPES), help="What kind of secret it is.")
@_scope_option
@click.option("--description", metavar="TEXT", help="What it is for, in a line. Not secret: the farm shows it.")
@click.option(
    "--metadata",
    "metadata_entries",
    metavar="KEY=VALUE",
    multiple=True,
    help="A non-secret annotation, shown as-is by `list --json` and `view` (repeatable).",
)
@click.option("--expires-at", metavar="ISO-8601", help="When the secret expires (informational: the farm only warns).")
@click.option("--value-file", metavar="FILE", help="Read the secret from a file ('-' for standard input).")
@click.option("--value-env", metavar="VARIABLE", help="Read the secret from this environment variable.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("write_credential")
@needs_client
def set_(
    client: FarmClient,
    name: str,
    type_: str,
    scope: str | None,
    description: str | None,
    metadata_entries: tuple[str, ...],
    expires_at: str | None,
    value_file: str | None,
    value_env: str | None,
    json_flag: bool,
) -> None:
    """Store a secret under NAME, or rotate it if NAME exists. The secret is never an argument.

    Without --value-file or --value-env it is asked for with a hidden prompt on a terminal, and read from standard
    input otherwise. One trailing line break is removed from standard input and from a file.

    \b
    dynfarm credential set git-readonly --type git_token                  # hidden prompt, asked twice
    dynfarm credential set git-readonly --type git_token --value-env GITLAB_TOKEN
    dynfarm credential set web-api --type generic --scope shared --value-file ./web-api.key
    pass show gitlab/readonly | dynfarm credential set git-readonly --type git_token
    """
    metadata = _metadata(metadata_entries)
    body: dict[str, Any] = {"name": name, "type": type_}
    for field, given in (("scope", scope), ("description", description), ("expires_at", expires_at)):
        if given is not None:
            body[field] = given
    if metadata_entries:
        body["metadata"] = metadata
    value = _read_value(value_file, value_env)
    try:
        saved = client.request("POST", "/credentials", json={**body, "value": value}, operation_id="write_credential")
    except FarmError as exc:
        raise _masked(exc, value) from None
    status = client.last_reply.status if client.last_reply else 201
    if json_flag:
        emit(saved, json_flag=True)
        return
    verb = "Rotated" if status == 200 else "Saved"
    shared = "shared " if saved.get("scope") == "shared" else ""
    click.echo(f"{verb} {shared}credential {saved['name']} ({saved['type']}, fingerprint {saved['fingerprint']})")


def _metadata(entries: tuple[str, ...]) -> dict[str, str]:
    """`--metadata KEY=VALUE`, repeated, as an object. An entry without `=` or without a key is a usage error."""
    metadata: dict[str, str] = {}
    for entry in entries:
        key, equals, value = entry.partition("=")
        if not equals or not key.strip():
            raise ConfigError("--metadata takes KEY=VALUE (an entry has no '=' or no key)")
        metadata[key.strip()] = value
    return metadata


def _read_value(value_file: str | None, value_env: str | None) -> str:
    """The secret, from exactly one source. Every error names the source, never the content."""
    if value_file is not None and value_env is not None:
        raise ConfigError("give --value-file or --value-env, not both")
    if value_env is not None:
        if value_env not in os.environ:
            raise ConfigError(f"--value-env: the environment variable {value_env} is not set")
        value = os.environ[value_env]
        where = f"the environment variable {value_env}"
    elif value_file is not None and value_file != "-":
        value = _without_final_newline(_decode(_read_file(value_file), f"--value-file {value_file}"))
        where = f"--value-file {value_file}"
    elif commands.is_interactive():
        # A terminal is never read as plain standard input: it would show the secret as it is typed.
        value = _prompt()
        where = "the prompt"
    else:
        value = _without_final_newline(_decode(_read_stdin(), "standard input"))
        where = "standard input"
    if not value.strip():
        raise ConfigError(f"the value from {where} is empty; nothing was saved")
    return value


def _prompt() -> str:
    """Ask twice with a hidden prompt (on stderr), so a typo is not stored as the secret."""
    first = click.prompt("Secret value (hidden)", hide_input=True, default="", show_default=False, err=True)
    if not first.strip():
        raise ConfigError("the value from the prompt is empty; nothing was saved")
    again = click.prompt("Repeat it", hide_input=True, default="", show_default=False, err=True)
    if again != first:
        raise ConfigError("the two values do not match; nothing was saved")
    return first


def _read_file(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ConfigError(f"cannot read --value-file {path}: {exc.strerror or exc.__class__.__name__}") from None


def _read_stdin() -> bytes:
    """Standard input as bytes: no newline translation, so a `\\r\\n` inside a secret stays as it was."""
    stream = getattr(sys.stdin, "buffer", None)
    return stream.read() if stream is not None else sys.stdin.read().encode("utf-8")


def _decode(data: bytes, where: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{where} is not UTF-8 text") from None


def _without_final_newline(text: str) -> str:
    """Remove the one line break `echo`, a heredoc or an editor adds at the end; anything else is the secret's."""
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _masked(exc: FarmError, value: str) -> FarmError:
    """The farm's error with the secret masked, should the farm ever quote it (the real one never does, §8 rule 5)."""

    def mask(item: Any) -> Any:
        if isinstance(item, str):
            return item.replace(value, "[redacted]")
        if isinstance(item, dict):
            return {key: mask(entry) for key, entry in item.items()}
        if isinstance(item, list):
            return [mask(entry) for entry in item]
        return item

    return FarmError(
        exc.status,
        exc.code,
        mask(exc.message),
        retryable=exc.retryable,
        details=mask(exc.details),
        retry_after=exc.retry_after,
        correlation_id=exc.correlation_id,
    )


# -- delete -------------------------------------------------------------------------------------------------------


@credential.command()
@click.argument("name")
@_scope_option
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@operation("delete_credential")
@needs_client
def delete(client: FarmClient, name: str, scope: str | None, yes: bool) -> None:
    """Delete a credential and its stored secret. Irreversible.

    Tasks already running keep the copy they were given; the next task or schedule that names it fails. Deleting a
    shared credential leaves the team's agents that declare it unlaunchable until it is written again.
    """
    shared = scope == "shared"
    commands.confirm(f"Delete {'shared ' if shared else ''}credential {name}? Its secret cannot be recovered.", yes=yes)
    client.request("DELETE", f"/credentials/{segment(name)}", params={"scope": scope}, operation_id="delete_credential")
    click.echo(f"deleted {'shared ' if shared else ''}credential {name}")
    note = "running tasks keep their copy; new tasks that name it will fail"
    if shared:
        note += ", and the team's agents that declare it cannot be launched until it is written again"
    click.echo(f"{note}. To store it again: `{_brand.NAME} credential set {name} --type TYPE`.", err=True)
