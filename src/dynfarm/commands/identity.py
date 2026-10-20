"""The read-only commands that say who you are to the farm and what the farm offers: `whoami`, `plugin list`,
`callback-key`.

- **`whoami`** is `GET /whoami` (`farm-api.md` §3.2): the key making the call, nothing secret. It is the first thing to
  run when a command answers 404 or 403 (whose objects, which scopes). The default output is a few labelled lines;
  `--json` is the farm's answer verbatim. Extra identity fields the farm may add (a display name, an e-mail) are shown
  as they come rather than dropped, because the farm, not this tool, decides what identifies a person.
- **`plugin list`** is the catalogue of plugins bundled in the agent image, with the agents each brings (the
  `agent_id` a task is launched with).
- **`callback-key`** prints the public key(s) that verify task callbacks. It is public key material, safe to show and
  to pipe: JSON as the farm sent it, or `--pem` for just the PEM text, ready for a file.

Text that came from the farm is cleaned of control characters before it reaches a terminal (`output`), so a subject or
a plugin name cannot rewrite the person's screen.
"""

from __future__ import annotations

import re
from typing import Any

import click

from dynfarm.errors import FarmError
from dynfarm.http import FarmClient, needs_client
from dynfarm.output import Column, emit
from dynfarm.registry import operation

JSON_HELP = "Print the farm's answer as JSON."

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: The `whoami` fields shown first, in this order and under these labels; anything else the farm adds follows.
_WHOAMI_FIELDS = (
    ("subject", "subject"),
    ("tenant", "tenant"),
    ("scopes", "scopes"),
    ("is_admin", "tenant admin"),
    ("free_prompt", "free prompt"),
    ("expires_at", "expires"),
)


# -- whoami --------------------------------------------------------------------------------------------------------


@click.command()
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("whoami")
@needs_client
def whoami(client: FarmClient, json_flag: bool) -> None:
    """Show who the farm thinks you are: subject, tenant and scopes of your token."""
    me = client.request("GET", "/whoami", operation_id="whoami")
    if json_flag:
        emit(me, json_flag=True)
        return
    known = {key for key, _ in _WHOAMI_FIELDS}
    rows = [(label, me[key]) for key, label in _WHOAMI_FIELDS if key in me]
    rows += [(key, value) for key, value in me.items() if key not in known and value is not None]
    width = max((len(label) for label, _ in rows), default=0)
    for label, value in rows:
        click.echo(f"{label + ':':<{width + 1}} {_show(value)}")


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_show(item) for item in value) or "(none)"
    if isinstance(value, dict):
        return _CONTROL.sub(" ", ", ".join(f"{key}={_show(item)}" for key, item in value.items()))
    return _CONTROL.sub(" ", str(value))


# -- plugin list ---------------------------------------------------------------------------------------------------


@click.group()
def plugin() -> None:
    """The plugins bundled in the agent image."""


PLUGIN_COLUMNS = (
    Column("name", "name"),
    Column("version", "version"),
    Column("commit", "commit"),
    Column("agents", lambda row: [agent.get("agent_id") for agent in row.get("agents") or []] or None),
)


@plugin.command("list")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("list_plugins")
@needs_client
def list_plugins(client: FarmClient, json_flag: bool) -> None:
    """List the plugins of the agent image and the agents each one brings."""
    emit(client.request("GET", "/plugins", operation_id="list_plugins"), json_flag=json_flag, table=PLUGIN_COLUMNS)


# -- callback-key --------------------------------------------------------------------------------------------------


@click.command("callback-key")
@click.option("--pem", is_flag=True, help="Print only the public key(s) as PEM, instead of the JSON.")
@operation("callback_key")
@needs_client
def callback_key(client: FarmClient, pem: bool) -> None:
    """Print the public key that verifies task callbacks (JWK as JSON, or --pem)."""
    answer = client.request("GET", "/callback-key", operation_id="callback_key")
    if not pem:
        emit(answer, json_flag=True)
        return
    keys = answer.get("keys") if isinstance(answer, dict) else None
    texts = [key["pem"] for key in keys or [] if isinstance(key, dict) and isinstance(key.get("pem"), str)]
    if not texts:
        raise FarmError(200, "response_invalid", "the farm sent no PEM key; use the JSON output")
    for text in texts:
        click.echo(text.rstrip("\n"))
