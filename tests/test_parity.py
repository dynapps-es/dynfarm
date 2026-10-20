"""The CLI and the API agree: every operation has a command, and what the CLI hard-codes about the API is still true.

Why this test exists: `dynfarm` is a hand-curated client (no code generation), so nothing but a test notices that the
API grew an endpoint, renamed a field, added a value to an enum or started requiring a new field. This file is that
test, and it is the gate the scheduled `api-drift` CI job runs against the live OpenAPI of the farm.

Which document is compared: `tests/fixtures/openapi.json` (the one the CLI was written against), or the file named by
`DYNFARM_PARITY_OPENAPI` (the CI job downloads the live one). The variable is read when the module is imported, because
the `conftest.py` fixtures scrub every `DYNFARM_*` variable from the environment before each test. A variable that
is set but points at nothing readable fails the run: a drift check that falls back to the pinned file is not one.

What is checked, each as a function that returns the problems it found so the tests at the bottom can prove it fails:

- every `operationId` has a command (`dynfarm.registry`) or an entry in `EXCLUDED` with the reason, and every command
  declares an operation that exists;
- the values of every `click.Choice` that mirrors an API enum equal the enum, and a new `Choice` nobody mapped is a
  failure, so a hard-coded list cannot go unchecked;
- the states `task watch` stops at are exactly the terminal states of the API (a new one would loop forever);
- the API does not require anything the pinned document did not (a new required field means the CLI cannot call it);
- the fields the tables read, and the keys `--json` prints, exist in the response schemas.

When this fails because the API changed on purpose: add the command or the exclusion, update the CLI, then refresh the
pinned document (`CONTRIBUTING.md`).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import click
import pytest
from fakefarm import ALICE

from dynfarm.cli import cli
from dynfarm.registry import load_all_commands, registered

PINNED = Path(__file__).parent / "fixtures" / "openapi.json"

#: Read at import on purpose: see the module docstring.
SPEC_PATH = os.environ.get("DYNFARM_PARITY_OPENAPI")

#: operationId -> why this API operation deliberately has no command. Empty: every operation has one.
EXCLUDED: dict[str, str] = {}

#: (command path, parameter name) of every `click.Choice` that mirrors an API enum -> (schema, property) it must equal.
CHOICES_FROM_THE_API: dict[tuple[tuple[str, ...], str], tuple[str, str]] = {
    (("agent", "create"), "scope"): ("AgentCreate", "scope"),
    (("agent", "edit"), "scope"): ("AgentCreate", "scope"),
    (("agent", "delete"), "scope"): ("AgentCreate", "scope"),
    (("agent", "list"), "scope"): ("AgentOut", "scope"),
    (("schedule", "create"), "overlap"): ("ScheduleCreate", "overlap"),
    (("credential", "set"), "type_"): ("CredentialCreate", "type"),
    (("credential", "set"), "scope"): ("CredentialCreate", "scope"),
    (("credential", "list"), "scope"): ("CredentialCreate", "scope"),
    (("credential", "view"), "scope"): ("CredentialCreate", "scope"),
    (("credential", "delete"), "scope"): ("CredentialCreate", "scope"),
}

#: `click.Choice`s that are not the API's: `dynfarm api -X`.
CHOICES_NOT_FROM_THE_API: set[tuple[tuple[str, ...], str]] = {(("api",), "method")}

#: Not terminal: the task can still move. Every other state of `TaskOut.state` ends `task watch`.
LIVE_STATES = {"pending", "queued", "running"}

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


# -- reading the OpenAPI ----------------------------------------------------------------------------------------


def load_spec(path: str | Path | None) -> dict[str, Any]:
    """The OpenAPI document at `path` (the pinned one when `path` is None); an unreadable file is an error."""
    target = Path(path) if path else PINNED
    try:
        return json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read the OpenAPI document {target}: {exc}") from exc


def operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """operationId -> the operation object (with its `method` and `path` added)."""
    found = {}
    for path, item in spec["paths"].items():
        for method in HTTP_METHODS:
            if method in item:
                found[item[method]["operationId"]] = {**item[method], "method": method, "path": path}
    return found


def deref(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def alternatives(spec: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The concrete schemas behind a `$ref` / `anyOf` / `oneOf` / `allOf` (a nullable object is two of them)."""
    schema = deref(spec, schema)
    parts = [sub for key in ("anyOf", "oneOf", "allOf") for sub in schema.get(key, [])]
    if not parts:
        return [schema]
    return [leaf for sub in parts for leaf in alternatives(spec, sub)]


def properties(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: prop for leaf in alternatives(spec, schema) for name, prop in leaf.get("properties", {}).items()}


def response_schema(spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
    operation = operations(spec)[operation_id]
    for status, answer in operation["responses"].items():
        if status.startswith("2"):
            return answer["content"]["application/json"]["schema"]
    raise KeyError(f"{operation_id} declares no 2xx JSON answer")


def row_schema(spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
    """What one row of a list answer looks like: `items` of a page, or the answer itself for a single object."""
    answer = response_schema(spec, operation_id)
    page = properties(spec, answer).get("items")
    if page is not None:
        return next(leaf["items"] for leaf in alternatives(spec, page) if "items" in leaf)
    return answer


def enum_of(spec: dict[str, Any], schema: str, name: str) -> set[str]:
    return set(spec["components"]["schemas"][schema]["properties"][name]["enum"])


def required_inputs(spec: dict[str, Any]) -> dict[str, set[str]]:
    """operationId -> what a caller must send: required body fields and required parameters."""
    found: dict[str, set[str]] = {}
    for operation_id, operation in operations(spec).items():
        needed = {f"param:{p['name']}" for p in operation.get("parameters", []) if p.get("required")}
        body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        for leaf in alternatives(spec, body) if body else []:
            needed |= {f"body:{name}" for name in leaf.get("required", [])}
        found[operation_id] = needed
    return found


# -- the checks (each returns the problems it found) -------------------------------------------------------------


def parity_problems(spec: dict[str, Any], wired: set[str], excluded: dict[str, str]) -> list[str]:
    ids = set(operations(spec))
    problems = []
    if missing := sorted(ids - wired - set(excluded)):
        problems.append(
            f"API operations with no command and no exclusion: {missing}. Add a command that declares "
            "@operation(...), or an entry in EXCLUDED with the reason."
        )
    if stale := sorted(wired - ids):
        problems.append(f"commands declare operations the API no longer has: {stale}. Remove or update them.")
    if gone := sorted(set(excluded) - ids):
        problems.append(f"EXCLUDED lists operations the API no longer has: {gone}.")
    if both := sorted(set(excluded) & wired):
        problems.append(f"EXCLUDED lists operations that now have a command: {both}. Drop the exclusion.")
    if unexplained := sorted(name for name, reason in excluded.items() if not reason.strip()):
        problems.append(f"EXCLUDED entries without a reason: {unexplained}.")
    return problems


def collect_choices(root: click.Command) -> dict[tuple[tuple[str, ...], str], tuple[str, ...]]:
    """(command path, parameter name) -> choices, for every `click.Choice` below `root` (lazy groups are loaded)."""
    found: dict[tuple[tuple[str, ...], str], tuple[str, ...]] = {}

    def visit(command: click.Command, path: tuple[str, ...], ctx: click.Context) -> None:
        for param in command.params:
            if isinstance(param.type, click.Choice) and param.name:
                found[(path, param.name)] = tuple(param.type.choices)
        if isinstance(command, click.Group):
            for name in command.list_commands(ctx):
                child = command.get_command(ctx, name)
                if child is not None:
                    visit(child, (*path, name), ctx)

    visit(root, (), click.Context(root))
    return found


def choice_problems(
    spec: dict[str, Any],
    choices: dict[tuple[tuple[str, ...], str], tuple[str, ...]],
    from_the_api: dict[tuple[tuple[str, ...], str], tuple[str, str]],
    not_from_the_api: set[tuple[tuple[str, ...], str]],
) -> list[str]:
    problems = []
    for key, values in sorted(choices.items()):
        where = f"`dynfarm {' '.join(key[0])} --{key[1].rstrip('_').replace('_', '-')}`"
        if key in not_from_the_api:
            continue
        if key not in from_the_api:
            problems.append(f"{where} offers a fixed list of values nobody mapped to an API enum: {list(values)}")
            continue
        schema, name = from_the_api[key]
        wanted = enum_of(spec, schema, name)
        if set(values) != wanted:
            problems.append(
                f"{where} offers {sorted(values)} but {schema}.{name} is {sorted(wanted)}: "
                f"not offered by the CLI {sorted(wanted - set(values))}, "
                f"unknown to the API {sorted(set(values) - wanted)}"
            )
    for key in sorted((set(from_the_api) | not_from_the_api) - set(choices)):
        problems.append(f"the table of choices lists {key}, which is no longer a click.Choice of any command")
    return problems


def terminal_state_problems(spec: dict[str, Any], terminal: set[str]) -> list[str]:
    wanted = enum_of(spec, "TaskOut", "state") - LIVE_STATES
    if terminal == wanted:
        return []
    return [
        f"`task watch` stops at {sorted(terminal)} but the API's final states are {sorted(wanted)}: "
        f"unknown to the CLI {sorted(wanted - terminal)}, unknown to the API {sorted(terminal - wanted)}"
    ]


def new_requirement_problems(spec: dict[str, Any], pinned: dict[str, Any]) -> list[str]:
    before, now = required_inputs(pinned), required_inputs(spec)
    return [
        f"{operation_id} now requires {sorted(now[operation_id] - before[operation_id])}, which the CLI does not send"
        for operation_id in sorted(set(before) & set(now))
        if now[operation_id] - before[operation_id]
    ]


def has_path(spec: dict[str, Any], schema: dict[str, Any], dotted: str) -> bool:
    head, _, rest = dotted.partition(".")
    found = properties(spec, schema).get(head)
    if found is None:
        return False
    return has_path(spec, found, rest) if rest else True


def column_problems(spec: dict[str, Any], tables: dict[str, tuple[Any, str]]) -> list[str]:
    """`tables`: name -> (the Column tuple, the operationId whose rows it shows)."""
    problems = []
    for name, (columns, operation_id) in sorted(tables.items()):
        row = row_schema(spec, operation_id)
        for column in columns:
            if isinstance(column.key, str) and not has_path(spec, row, column.key):
                problems.append(f"{name} reads `{column.key}`, which a row of {operation_id} no longer has")
    return problems


def stray_keys(spec: dict[str, Any], schema: dict[str, Any], value: Any, where: str = "$") -> list[str]:
    """Where `value` (parsed JSON) has a key the response schema does not declare. Free-form objects are not checked."""
    found: list[str] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            for leaf in alternatives(spec, schema):
                if "items" in leaf:
                    found += stray_keys(spec, leaf["items"], item, f"{where}[{index}]")
                    break
    elif isinstance(value, dict):
        declared = properties(spec, schema)
        if not declared:
            return found
        for key, item in value.items():
            if key not in declared:
                found.append(f"{where}.{key}")
            else:
                found += stray_keys(spec, declared[key], item, f"{where}.{key}")
    return found


def assert_none(problems: list[str]) -> None:
    """Fail with one problem per line: the scheduled drift job's log must say which operation or value drifted."""
    assert not problems, "the CLI and the API disagree:\n  - " + "\n  - ".join(problems)


# -- the real thing -----------------------------------------------------------------------------------------------

SPEC = load_spec(SPEC_PATH)


def table_columns() -> dict[str, tuple[Any, str]]:
    from dynfarm.commands import agent, credential, identity, schedule, task

    return {
        "AGENT_COLUMNS": (agent.AGENT_COLUMNS, "list_agents"),
        "TASK_COLUMNS": (task.TASK_COLUMNS, "list_tasks"),
        "CREATED_COLUMNS": (task.CREATED_COLUMNS, "create_task"),
        "SCHEDULE_COLUMNS": (schedule.SCHEDULE_COLUMNS, "list_schedules"),
        "FIRED_COLUMNS": (schedule.FIRED_COLUMNS, "list_schedule_tasks"),
        "CREDENTIAL_COLUMNS": (credential.CREDENTIAL_COLUMNS, "list_credentials"),
        "PLUGIN_COLUMNS": (identity.PLUGIN_COLUMNS, "list_plugins"),
    }


@pytest.fixture(scope="module")
def wired() -> set[str]:
    load_all_commands()
    return set(registered())


def test_every_api_operation_has_a_command_or_a_reasoned_exclusion(wired):
    assert_none(parity_problems(SPEC, wired, EXCLUDED))


def test_every_choice_that_mirrors_an_api_enum_equals_it():
    assert_none(choice_problems(SPEC, collect_choices(cli), CHOICES_FROM_THE_API, CHOICES_NOT_FROM_THE_API))


def test_task_watch_stops_at_exactly_the_terminal_states_of_the_api():
    from dynfarm.taskbody import TERMINAL_STATES

    assert_none(terminal_state_problems(SPEC, set(TERMINAL_STATES)))


def test_the_api_requires_nothing_the_cli_does_not_send():
    assert_none(new_requirement_problems(SPEC, load_spec(None)))


def test_every_column_the_tables_read_exists_in_the_response_schemas():
    assert_none(column_problems(SPEC, table_columns()))


def json_cases(fake_farm, run_cli) -> list[tuple[list[str], str]]:
    """Arrange one of each resource through the CLI; name each command and the operation whose answer it prints."""
    task_id = fake_farm.seed_task(ALICE, state="succeeded", logs=["one", "two"])
    created = json.loads(
        run_cli(["schedule", "create", "--prompt", "hi", "--cron", "0 8 * * *", "--timezone", "UTC", "--json"]).stdout
    )
    schedule_id = created["schedule_id"]
    assert run_cli(["credential", "set", "parity-key", "--type", "generic"], input="not-a-real-secret\n").exit_code == 0
    assert run_cli(["agent", "create", "parity-agent", "--scope", "personal", "--instructions", "hi"]).exit_code == 0
    return [
        (["whoami"], "whoami"),
        (["plugin", "list"], "list_plugins"),
        (["callback-key"], "callback_key"),  # prints the JWK as JSON already: it has no --json
        (["agent", "list"], "list_agents"),
        (["agent", "view", "parity-agent"], "get_agent"),
        (["task", "list"], "list_tasks"),
        (["task", "view", task_id], "get_task"),
        (["task", "logs", task_id], "get_logs"),
        (["task", "result", task_id], "get_result"),
        (["task", "run", "--prompt", "hi"], "create_task"),
        (["schedule", "list"], "list_schedules"),
        (["schedule", "view", schedule_id], "get_schedule"),
        (["schedule", "runs", schedule_id], "list_schedule_tasks"),
        (["schedule", "pause", schedule_id], "pause_schedule"),
        (["credential", "list"], "list_credentials"),
        (["credential", "view", "parity-key"], "get_credential"),
    ]


def test_every_json_output_has_only_keys_the_response_schema_declares(fake_farm, run_cli):
    checked = 0
    for args, operation_id in json_cases(fake_farm, run_cli):
        result = run_cli(args if args == ["callback-key"] else [*args, "--json"])
        assert result.exit_code == 0, (args, result.output)
        stray = stray_keys(SPEC, response_schema(SPEC, operation_id), json.loads(result.stdout))
        assert_none(
            [
                f"`dynfarm {' '.join(args)} --json` prints {key}, which {operation_id} no longer declares"
                for key in stray
            ]
        )
        checked += 1
    assert checked == 16


# -- proof that the checks can fail (a check that cannot is not a gate) ----------------------------------------------


@pytest.fixture
def mutable() -> dict[str, Any]:
    return copy.deepcopy(load_spec(None))


def test_a_spec_with_an_operation_nobody_wired_fails_the_parity_check(mutable, wired):
    mutable["paths"]["/farm/api/v1/webhooks"] = {"get": {"operationId": "list_webhooks", "responses": {"200": {}}}}
    problems = parity_problems(mutable, wired, EXCLUDED)
    assert len(problems) == 1 and "list_webhooks" in problems[0]


def test_a_spec_that_dropped_an_operation_makes_its_command_stale(mutable, wired):
    del mutable["paths"]["/farm/api/v1/whoami"]
    problems = parity_problems(mutable, wired, EXCLUDED)
    assert len(problems) == 1 and "whoami" in problems[0] and "no longer has" in problems[0]


def test_an_exclusion_needs_a_reason_and_a_real_unwired_operation(wired):
    spec = load_spec(None)
    assert parity_problems(spec, wired - {"whoami"}, {"whoami": "the farm can tell who you are another way"}) == []
    assert any("without a reason" in p for p in parity_problems(spec, wired - {"whoami"}, {"whoami": " "}))
    assert any("no longer has" in p for p in parity_problems(spec, wired, {"gone_op": "x"}))
    assert any("now have a command" in p for p in parity_problems(spec, wired, {"whoami": "x"}))


def test_an_extra_value_in_an_api_enum_fails_the_choice_check(mutable):
    mutable["components"]["schemas"]["CredentialCreate"]["properties"]["type"]["enum"].append("ssh_key")
    problems = choice_problems(mutable, collect_choices(cli), CHOICES_FROM_THE_API, CHOICES_NOT_FROM_THE_API)
    assert problems and all("ssh_key" in p for p in problems)


def test_a_value_the_api_dropped_fails_the_choice_check(mutable):
    mutable["components"]["schemas"]["ScheduleCreate"]["properties"]["overlap"]["enum"].remove("allow")
    problems = choice_problems(mutable, collect_choices(cli), CHOICES_FROM_THE_API, CHOICES_NOT_FROM_THE_API)
    assert len(problems) == 1 and "allow" in problems[0] and "schedule create" in problems[0]


def test_a_new_fixed_list_of_values_nobody_mapped_fails_the_choice_check():
    group = click.Group("tool")

    @group.command("go")
    @click.option("--mode", type=click.Choice(["fast", "slow"]))
    def go(mode):  # pragma: no cover - never invoked, only inspected
        pass

    choices = collect_choices(group)
    assert choices == {(("go",), "mode"): ("fast", "slow")}
    problems = choice_problems(SPEC, choices, {}, set())
    assert len(problems) == 1 and "nobody mapped" in problems[0]


def test_a_choice_that_stopped_existing_leaves_the_table_stale():
    problems = choice_problems(SPEC, {}, CHOICES_FROM_THE_API, CHOICES_NOT_FROM_THE_API)
    assert len(problems) == len(CHOICES_FROM_THE_API) + len(CHOICES_NOT_FROM_THE_API)


def test_a_new_final_state_in_the_api_fails_the_terminal_state_check(mutable):
    from dynfarm.taskbody import TERMINAL_STATES

    mutable["components"]["schemas"]["TaskOut"]["properties"]["state"]["enum"].append("timed_out")
    problems = terminal_state_problems(mutable, set(TERMINAL_STATES))
    assert len(problems) == 1 and "timed_out" in problems[0]


def test_a_field_the_api_began_to_require_fails_the_requirement_check(mutable):
    mutable["components"]["schemas"]["ScheduleCreate"]["required"].append("webhook_url")
    problems = new_requirement_problems(mutable, load_spec(None))
    assert problems == ["create_schedule now requires ['body:webhook_url'], which the CLI does not send"]


def test_a_new_required_query_parameter_fails_the_requirement_check(mutable):
    mutable["paths"]["/farm/api/v1/tasks"]["get"]["parameters"].append(
        {"name": "region", "in": "query", "required": True, "schema": {"type": "string"}}
    )
    problems = new_requirement_problems(mutable, load_spec(None))
    assert problems == ["list_tasks now requires ['param:region'], which the CLI does not send"]


def test_a_field_the_api_removed_fails_the_column_check(mutable):
    del mutable["components"]["schemas"]["TaskOut"]["properties"]["owner"]
    problems = column_problems(mutable, table_columns())
    assert problems == ["TASK_COLUMNS reads `owner`, which a row of list_tasks no longer has"]


def test_a_nested_field_the_api_removed_fails_the_column_check(mutable):
    recurrence = deref(mutable, mutable["components"]["schemas"]["ScheduleOut"]["properties"]["recurrence"])
    del recurrence["properties"]["cron"]
    problems = column_problems(mutable, table_columns())
    assert problems == ["SCHEDULE_COLUMNS reads `recurrence.cron`, which a row of list_schedules no longer has"]


def test_a_key_the_schema_does_not_declare_fails_the_json_check(mutable):
    del mutable["components"]["schemas"]["WhoAmI"]["properties"]["tenant"]
    answer = {"subject": "alice", "tenant": "acme", "scopes": []}
    assert stray_keys(mutable, response_schema(mutable, "whoami"), answer) == ["$.tenant"]


def test_the_json_check_looks_inside_lists_and_nested_objects(mutable):
    del mutable["components"]["schemas"]["TaskOut"]["properties"]["agent_id"]
    page = {"items": [{"task_id": "t1", "agent_id": "x"}], "next_cursor": None}
    assert stray_keys(mutable, response_schema(mutable, "list_tasks"), page) == ["$.items[0].agent_id"]


def test_an_unreadable_openapi_file_is_an_error_not_a_fallback(tmp_path):
    with pytest.raises(RuntimeError, match="cannot read the OpenAPI document"):
        load_spec(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(RuntimeError, match="cannot read the OpenAPI document"):
        load_spec(broken)


def test_the_spec_under_test_is_the_one_the_environment_names():
    expected = load_spec(SPEC_PATH)
    assert SPEC == expected
    if SPEC_PATH is None:
        assert SPEC == load_spec(PINNED)
