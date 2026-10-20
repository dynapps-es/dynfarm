"""`dynfarm agent`: see which agents the farm offers and manage your own and your team's.

    agent list     GET    /agents            agent create SLUG  POST   /agents
    agent view     GET    /agents/{slug}     agent edit SLUG    PATCH  /agents/{slug}
    agent delete   DELETE /agents/{slug}

An agent has three levels (`farm-api.md` §7.6): `platform` ones are the farm's and read-only, `team` ones are shared
by the tenant and only a tenant admin writes them, `personal` ones are yours alone. The farm decides who may do what
(`403 agent_read_only`, `403 insufficient_scope`, `404` for what is not yours); this tool shows its answer.

Choices worth knowing:

- **Flags cover the common fields, the rest goes in a file.** `--description`, `--instructions`/`--instructions-file`,
  `--plugin-agent` and `--requires-workspace` are flags. Everything else an agent can carry — `mcp_servers`,
  `credentials`, `env_credentials`, `egress`, `limits`, `image`, `services`, `resources`, `sandbox`, `profile`,
  `work_size` — goes in `--from-file FILE` (`-` for standard input): an `AgentCreate` (or `AgentUpdate`) JSON
  document. A flag overrides the same field of the file. A flag per field would be a second copy of the OpenAPI.
- **`create` needs the level spelled out** (`--scope personal|team`, or `scope` in the file). A team agent is seen and
  launched by everybody in the tenant, so the tool never picks one for you.
- **`edit` sends only what you gave.** `slug` and `scope` never change, so there `--scope` only says *which* agent to
  edit when your personal agent and your team's share a slug (the farm looks at the personal one first). To clear a
  field, set it to `null` in the file (`{"instructions": null}`): no flag means "empty".
- **`delete` asks first**, like every command that removes something (`--yes` in scripts). The farm refuses while a
  schedule that has not finished still launches the agent (`409 agent_in_use_by_schedule`).
- Instructions are stored and returned by the farm and every person of the tenant can read a team agent's: keep
  secrets out of them. Text that comes from the farm is cleaned of control characters before it reaches the terminal.
"""

from __future__ import annotations

import json
from typing import Any

import click

from dynfarm.commands import confirm
from dynfarm.errors import ConfigError
from dynfarm.http import FarmClient, needs_client, segment
from dynfarm.inputs import read_json_object, read_text
from dynfarm.output import Column, clean_text, emit, stdout_is_terminal
from dynfarm.registry import operation

JSON_HELP = "Print the farm's answer as JSON."

#: The levels one can write (`platform` is the farm's own and read-only).
WRITABLE_SCOPES = click.Choice(["team", "personal"])

AGENT_COLUMNS = (
    Column("slug", "slug"),
    Column("scope", "scope"),
    Column("owner", "owner"),
    Column("description", "description"),
)


@click.group()
def agent() -> None:
    """See the agents the farm offers and manage your own and your team's."""


# -- the flags create and edit share -------------------------------------------------------------------------------


def agent_body_options(fn: Any) -> Any:
    """The flags that describe an agent, for `create` and `edit`; the callback gets them as keyword arguments."""
    options = [
        click.option(
            "--from-file",
            metavar="FILE",
            help="A complete agent as JSON ('-' for standard input), for what has no flag (mcp_servers, credentials, "
            "env_credentials, egress, limits, image...). The flags override its fields.",
        ),
        click.option("--description", metavar="TEXT", help="What the agent is for (shown in `agent list`)."),
        click.option("--instructions", metavar="TEXT", help="The agent's base prompt, sent in front of every input."),
        click.option(
            "--instructions-file",
            metavar="FILE",
            help="Read the instructions from a file ('-' for standard input).",
        ),
        click.option(
            "--plugin-agent",
            metavar="PLUGIN:AGENT",
            help="Build on an agent of a plugin (see `dynfarm plugin list`), e.g. developer-skills:code-reviewer.",
        ),
        click.option(
            "--requires-workspace/--no-requires-workspace",
            default=None,
            help="Whether a task of this agent has to bring a workspace (a repository).",
        ),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def build_agent_body(
    *,
    from_file: str | None,
    description: str | None,
    instructions: str | None,
    instructions_file: str | None,
    plugin_agent: str | None,
    requires_workspace: bool | None,
) -> dict[str, Any]:
    """The `AgentCreate`/`AgentUpdate` fields these flags and the optional file describe: the file is the base, each
    flag replaces its field. Refuses, before any request, only what cannot be read or is given twice; the farm
    validates the agent itself and its answer is shown as it comes."""
    if from_file == "-" and instructions_file == "-":
        raise ConfigError("only one of --from-file, --instructions-file can read standard input")
    body = read_json_object(from_file, "--from-file", "an agent") if from_file else {}
    if instructions is not None and instructions_file is not None:
        raise ConfigError("give --instructions or --instructions-file, not both")
    if instructions_file is not None:
        instructions = read_text(instructions_file, "--instructions-file")
    if instructions is not None and not instructions.strip():
        raise ConfigError("the instructions are empty")
    for field, value in (
        ("description", description),
        ("instructions", instructions),
        ("plugin_agent", plugin_agent),
        ("requires_workspace", requires_workspace),
    ):
        if value is not None:
            body[field] = value
    return body


# -- list ---------------------------------------------------------------------------------------------------------


@agent.command("list")
@click.option(
    "--scope",
    type=click.Choice(["platform", "team", "personal"]),
    help="Only agents of this level.",
)
@click.option("--owner", metavar="OWNER", help="`me`: only your own personal agents.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("list_agents")
@needs_client
def list_(client: FarmClient, scope: str | None, owner: str | None, json_flag: bool) -> None:
    """List the agents you can launch: the farm's, your team's and your own."""
    params = {key: value for key, value in (("scope", scope), ("owner", owner)) if value is not None}
    page = client.request("GET", "/agents", params=params, operation_id="list_agents")
    emit(page, json_flag=json_flag, table=AGENT_COLUMNS)
    if not json_flag and page["items"] and stdout_is_terminal():
        count = len(page["items"])
        click.echo(f"{count} agent{'' if count == 1 else 's'}", err=True)


# -- view ---------------------------------------------------------------------------------------------------------


@agent.command()
@click.argument("slug")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("get_agent")
@needs_client
def view(client: FarmClient, slug: str, json_flag: bool) -> None:
    """Show an agent: what it is for, its instructions, what it may touch and its limits."""
    found = client.request("GET", f"/agents/{segment(slug)}", operation_id="get_agent")
    if json_flag:
        emit(found, json_flag=True)
        return
    _print_agent(found)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fields(found: dict[str, Any]) -> list[tuple[str, Any]]:
    capabilities = found.get("capabilities") or {}
    servers = found.get("mcp_servers") or []
    env = found.get("env_credentials") or {}
    limits = found.get("default_limits") or {}
    return [
        ("slug", found.get("slug")),
        ("scope", found.get("scope")),
        ("owner", found.get("owner")),
        ("description", found.get("description")),
        ("plugin agent", found.get("plugin_agent")),
        ("harness", found.get("harness")),
        ("model", found.get("model_alias")),
        ("requires workspace", {True: "yes", False: "no"}.get(found.get("requires_workspace"))),
        ("credentials", ", ".join(capabilities.get("credentials") or [])),
        ("egress", ", ".join(capabilities.get("egress") or [])),
        ("env credentials", ", ".join(f"{name}={credential}" for name, credential in env.items())),
        ("mcp servers", ", ".join(_server(server) for server in servers)),
        ("limits", ", ".join(f"{key}={value}" for key, value in limits.items())),
        ("image", found.get("image")),
        ("profile", found.get("profile")),
        ("work size", found.get("work_size")),
        ("resources", _compact(found["resources"]) if found.get("resources") else None),
        ("sandbox", _compact(found["sandbox"]) if found.get("sandbox") else None),
        ("services", _compact(found["services"]) if found.get("services") else None),
    ]


def _server(server: dict[str, Any]) -> str:
    """`docs (credential mcp-docs)`: a server's name and the credential it uses, never more than the farm says."""
    return f"{server.get('name')} (credential {server.get('credential')})"


def _print_agent(found: dict[str, Any]) -> None:
    rows = [(label, value) for label, value in _fields(found) if value not in (None, "")]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        click.echo(f"{label + ':':<{width}} {clean_text(str(value))}")
    if found.get("instructions"):
        click.echo("instructions:")
        for line in clean_text(found["instructions"], multiline=True).splitlines():
            click.echo(f"  {line}")


# -- create -------------------------------------------------------------------------------------------------------


@agent.command()
@click.argument("slug")
@click.option("--scope", type=WRITABLE_SCOPES, help="`personal`: yours alone. `team`: the whole tenant (admins only).")
@agent_body_options
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("create_agent")
@needs_client
def create(client: FarmClient, slug: str, scope: str | None, json_flag: bool, **fields: Any) -> None:
    """Create an agent: a name to launch with `task run`, and its instructions and/or a plugin agent.

    \b
    dynfarm agent create web-manager --scope personal --instructions-file web-manager.md
    dynfarm agent create reviewer --scope team --plugin-agent developer-skills:code-reviewer --requires-workspace
    dynfarm agent create etl --scope personal --from-file etl.json      # credentials, mcp_servers, limits...
    """
    body = build_agent_body(**fields)
    body["slug"] = slug
    if scope is not None:
        body["scope"] = scope
    if not body.get("scope"):
        raise ConfigError("say who the agent is for: --scope personal (yours alone) or --scope team (the tenant)")
    created = client.request("POST", "/agents", json=body, operation_id="create_agent")
    emit(created, json_flag=json_flag, table=AGENT_COLUMNS)
    if not json_flag:
        click.echo(f"launch it with `dynfarm task run {created['slug']} --input TEXT`", err=True)


# -- edit ---------------------------------------------------------------------------------------------------------


@agent.command()
@click.argument("slug")
@click.option(
    "--scope",
    type=WRITABLE_SCOPES,
    help="Which agent to edit when your personal one and your team's share the slug (default: the personal one).",
)
@agent_body_options
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("update_agent")
@needs_client
def edit(client: FarmClient, slug: str, scope: str | None, json_flag: bool, **fields: Any) -> None:
    """Change an agent. Only what you give is sent; the rest stays as it is.

    \b
    dynfarm agent edit web-manager --description "Keeps the website up to date"
    dynfarm agent edit web-manager --instructions-file web-manager.md
    echo '{"instructions": null, "plugin_agent": "docs:writer"}' | dynfarm agent edit web-manager --from-file -
    """
    body = build_agent_body(**fields)
    for fixed in ("slug", "scope"):
        if fixed in body:
            raise ConfigError(f"{fixed} of an agent never changes; use --scope to say which agent to edit")
    if not body:
        raise ConfigError("nothing to change: give a flag (--description, --instructions...) or --from-file")
    updated = client.request(
        "PATCH", f"/agents/{segment(slug)}", params={"scope": scope}, json=body, operation_id="update_agent"
    )
    emit(updated, json_flag=json_flag, table=AGENT_COLUMNS)


# -- delete -------------------------------------------------------------------------------------------------------


@agent.command()
@click.argument("slug")
@click.option(
    "--scope",
    type=WRITABLE_SCOPES,
    help="Which agent to delete when your personal one and your team's share the slug (default: the personal one).",
)
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@operation("delete_agent")
@needs_client
def delete(client: FarmClient, slug: str, scope: str | None, yes: bool) -> None:
    """Delete an agent. Tasks already accepted keep running; its slug is free to be created again."""
    confirm(f"Delete {scope + ' ' if scope else ''}agent {slug}?", yes=yes)
    client.request("DELETE", f"/agents/{segment(slug)}", params={"scope": scope}, operation_id="delete_agent")
    click.echo(f"deleted agent {slug}")
