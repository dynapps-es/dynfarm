"""`dynfarm schedule`: a task that the farm launches again and again, created and operated from the terminal.

    schedule list         GET    /schedules             schedule create [AGENT]  POST /schedules
    schedule view ID      GET    /schedules/{id}        schedule runs ID         GET  /schedules/{id}/tasks
    schedule fire ID      POST   /schedules/{id}/fire  schedule pause ID        POST /schedules/{id}/pause
    schedule delete ID    DELETE /schedules/{id}       schedule resume ID       POST /schedules/{id}/resume

A schedule is a `task` (exactly the body of `POST /tasks`) plus a `recurrence` (a 5-field cron in an IANA timezone,
optionally bounded by `starts_at`, `ends_at` or `max_runs`) and an `overlap` policy (`farm-api.md` §7.9). It belongs to
the person who created it, and so does every task it fires, whoever pauses or fires it later. Someone else's schedule is
a `404`, not a `403`.

Choices worth knowing:

- **The task is written with the flags of `task run`** (`AGENT`, `--prompt`/`--prompt-file`, `--input`/`--input-file`,
  `--scope`, `--session-ref`, `--body-file`): one builder (`dynfarm.taskbody`) serves both commands, so a task that
  works as a one-off works as a schedule. There is no `--correlation-id`: each firing generates its own.
- **The recurrence is `--cron` and `--timezone`, or `--recurrence-file`** (a `RecurrenceIn` JSON object, `-` for
  standard input), and the flags override the same field of the file. The timezone has no default on purpose: a cron
  means different instants in different zones, and guessing the person's would fire a report at the wrong hour. The
  farm checks the cron and the zone (`422 cron_invalid`, `422 timezone_invalid`); this tool only refuses to send a
  recurrence that lacks one.
- **At most one input reads standard input**: `--prompt-file -`, `--input-file -`, `--body-file -` and
  `--recurrence-file -` are checked together, before anything is read, because the second reader would get nothing.
- **`runs` has no `--all`**: the farm answers every task the schedule fired in one reply, never paginated
  (`farm-api.md` §7.9), so there is no page to follow. `--limit N` keeps the newest N of them on this side.
- **`fire` creates a task now** (and the farm records it as `trigger: manual`); it does not move `next_fire_at` or
  count against `max_runs`. It refuses with `409 schedule_not_active` or `409 schedule_overlap` instead of skipping
  quietly, as a cron firing would. A launch is never repeated by itself, not even after a network error.
- **`delete` asks first**, like every command that removes something (`--yes` in scripts). It stops the schedule; the
  tasks it already fired are untouched.
"""

from __future__ import annotations

from typing import Any

import click

from dynfarm.commands import confirm
from dynfarm.errors import ConfigError
from dynfarm.http import FarmClient, needs_client, segment
from dynfarm.inputs import read_json_object
from dynfarm.output import Column, clean_text, emit
from dynfarm.registry import operation
from dynfarm.taskbody import STORED_WARNING, TaskFlags, build_task_body, stores_text, task_body_options

JSON_HELP = "Print the farm's answer as JSON."


def _agent_or_prompt(row: dict[str, Any]) -> str:
    """What the schedule's task runs: the agent's name, or `(free prompt)`."""
    task_body = row.get("task") or {}
    return task_body.get("agent_id") or ("(free prompt)" if task_body.get("prompt") else "")


SCHEDULE_COLUMNS = (
    Column("id", "schedule_id"),
    Column("state", "state"),
    Column("cron", "recurrence.cron"),
    Column("timezone", "recurrence.timezone"),
    Column("next fire", "next_fire_at", "date"),
    Column("runs", "runs_count"),
    Column("task", _agent_or_prompt),
    Column("owner", "owner"),
)

#: The tasks a schedule fired (`runs`) and the one `fire` creates are both full task objects.
FIRED_COLUMNS = (
    Column("id", "task_id"),
    Column("state", "state"),
    Column("trigger", "trigger"),
    Column("agent", "agent_id"),
    Column("created", "created_at", "date"),
)


@click.group()
def schedule() -> None:
    """Have the farm launch a task on a recurring schedule."""


def _note(message: str) -> None:
    click.echo(message, err=True)


def _id_arg(command: Any) -> Any:
    return click.argument("schedule_id", metavar="ID")(command)


def _path(schedule_id: str, suffix: str = "") -> str:
    return f"/schedules/{segment(schedule_id)}{suffix}"


# -- list ---------------------------------------------------------------------------------------------------------


@schedule.command("list")
@click.option("--owner", metavar="OWNER", help="`me`, or (tenant admins) a person's subject.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("list_schedules")
@needs_client
def list_(client: FarmClient, owner: str | None, json_flag: bool) -> None:
    """List schedules, newest first. A tenant admin sees the whole tenant's; --owner me narrows it to their own."""
    params = {"owner": owner} if owner is not None else None
    found = client.request("GET", "/schedules", params=params, operation_id="list_schedules")
    emit(found, json_flag=json_flag, table=SCHEDULE_COLUMNS)


# -- view ---------------------------------------------------------------------------------------------------------


def _recurrence_line(row: dict[str, Any]) -> str | None:
    recurrence = row.get("recurrence") or {}
    if not recurrence.get("cron"):
        return None
    return f"{recurrence['cron']} ({recurrence.get('timezone')})"


def _last_fire(row: dict[str, Any]) -> str | None:
    """`ok at 2026-10-20T08:00:00Z`, or the reason a firing did not create a task."""
    state = row.get("last_fire_state")
    if not state:
        return None
    text = f"{state} at {row.get('last_fired_at')}" if row.get("last_fired_at") else str(state)
    return f"{text}: {row['last_fire_error']}" if row.get("last_fire_error") else text


def _fields(row: dict[str, Any]) -> list[tuple[str, Any]]:
    recurrence = row.get("recurrence") or {}
    return [
        ("id", row.get("schedule_id")),
        ("state", row.get("state")),
        ("recurrence", _recurrence_line(row)),
        ("starts", recurrence.get("starts_at")),
        ("ends", recurrence.get("ends_at")),
        ("max runs", recurrence.get("max_runs")),
        ("overlap", row.get("overlap")),
        ("next fire", row.get("next_fire_at")),
        ("last fire", _last_fire(row)),
        ("runs", row.get("runs_count")),
        ("missed runs", row.get("missed_runs")),
        ("owner", row.get("owner")),
        ("created", row.get("created_at")),
        ("agent", _agent_or_prompt(row) or None),
        ("scope", (row.get("task") or {}).get("scope")),
    ]


def _print_schedule(row: dict[str, Any], *, json_flag: bool) -> None:
    """The schedule as labelled lines (the task's prompt and input as indented blocks after them), or as JSON."""
    if json_flag:
        emit(row, json_flag=True)
        return
    rows = [(label, value) for label, value in _fields(row) if value not in (None, "")]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        click.echo(f"{label + ':':<{width}} {clean_text(str(value))}")
    for label in ("prompt", "input"):
        text = (row.get("task") or {}).get(label)
        if text:
            click.echo(f"{label}:")
            for line in clean_text(str(text), multiline=True).splitlines():
                click.echo(f"  {line}")


@schedule.command()
@_id_arg
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("get_schedule")
@needs_client
def view(client: FarmClient, schedule_id: str, json_flag: bool) -> None:
    """Show a schedule: when it fires next, how its last firing went, and the task it launches."""
    _print_schedule(client.request("GET", _path(schedule_id), operation_id="get_schedule"), json_flag=json_flag)


# -- create -------------------------------------------------------------------------------------------------------


def _stdin_readers(*pairs: tuple[str, str | None]) -> list[str]:
    return [flag for flag, source in pairs if source == "-"]


def build_recurrence(
    *,
    recurrence_file: str | None,
    cron: str | None,
    timezone: str | None,
    starts_at: str | None,
    ends_at: str | None,
    max_runs: int | None,
) -> dict[str, Any]:
    """The `RecurrenceIn` for these flags: the file is the base, each flag replaces its field.

    Raises `ConfigError` (exit 2) when the result lacks a cron or a timezone. It does not judge them: the farm answers
    `422 cron_invalid` / `422 timezone_invalid` with its own wording.
    """
    recurrence = (
        read_json_object(recurrence_file, "--recurrence-file", "a recurrence") if recurrence_file is not None else {}
    )
    for field, value in (
        ("cron", cron),
        ("timezone", timezone),
        ("starts_at", starts_at),
        ("ends_at", ends_at),
        ("max_runs", max_runs),
    ):
        if value is not None:
            recurrence[field] = value
    if not recurrence.get("cron"):
        raise ConfigError("say when to fire: --cron '0 8 * * 1-5' (five fields, no seconds) or --recurrence-file")
    if not recurrence.get("timezone"):
        raise ConfigError("say which timezone the cron is in: --timezone Europe/Madrid (an IANA name)")
    return recurrence


@schedule.command()
@click.argument("agent", required=False)
@task_body_options(correlation_id=False)
@click.option("--cron", metavar="EXPR", help="When to fire: five fields (minute hour day month weekday), no seconds.")
@click.option("--timezone", metavar="TZ", help="The IANA timezone the cron is evaluated in, e.g. Europe/Madrid.")
@click.option(
    "--recurrence-file",
    metavar="FILE",
    help="The recurrence as JSON ('-' for standard input): cron, timezone, starts_at, ends_at, max_runs.",
)
@click.option("--starts-at", metavar="TIME", help="Do not fire before this ISO 8601 time (default: now).")
@click.option("--ends-at", metavar="TIME", help="Stop firing from this ISO 8601 time on; the schedule finishes.")
@click.option(
    "--max-runs", type=click.IntRange(min=1), metavar="N", help="Stop after N firings; the schedule finishes."
)
@click.option(
    "--overlap",
    type=click.Choice(["skip", "allow"]),
    help="A firing due while the previous task still runs: `skip` drops it (the farm's default), `allow` runs both.",
)
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("create_schedule")
@needs_client
def create(
    client: FarmClient,
    agent: str | None,
    task_flags: TaskFlags,
    cron: str | None,
    timezone: str | None,
    recurrence_file: str | None,
    starts_at: str | None,
    ends_at: str | None,
    max_runs: int | None,
    overlap: str | None,
    json_flag: bool,
) -> None:
    """Create a schedule: a task (an AGENT with --input, or a --prompt) and when to launch it.

    \b
    dynfarm schedule create developer-skills:code-reviewer --input "Review my open MRs." \\
        --cron "0 8 * * 1-5" --timezone Europe/Madrid
    dynfarm schedule create --prompt-file digest.txt --scope docs --cron "0 7 * * 1" --timezone UTC --max-runs 10
    dynfarm schedule create --body-file task.json --recurrence-file recurrence.json --overlap allow
    """
    readers = _stdin_readers(
        ("--prompt-file", task_flags.prompt_file),
        ("--input-file", task_flags.input_file),
        ("--body-file", task_flags.body_file),
        ("--recurrence-file", recurrence_file),
    )
    if len(readers) > 1:
        raise ConfigError(f"only one of {', '.join(readers)} can read standard input")
    task_body = build_task_body(task_flags, agent=agent)
    body: dict[str, Any] = {
        "task": task_body,
        "recurrence": build_recurrence(
            recurrence_file=recurrence_file,
            cron=cron,
            timezone=timezone,
            starts_at=starts_at,
            ends_at=ends_at,
            max_runs=max_runs,
        ),
    }
    if overlap is not None:
        body["overlap"] = overlap
    if stores_text(task_body):
        _note(STORED_WARNING)
    created = client.request("POST", "/schedules", json=body, operation_id="create_schedule")
    emit(created, json_flag=json_flag, table=SCHEDULE_COLUMNS)
    if not json_flag:
        _note(f"pause it with `dynfarm schedule pause {created['schedule_id']}`")


# -- runs ---------------------------------------------------------------------------------------------------------


@schedule.command()
@_id_arg
@click.option("--limit", type=click.IntRange(min=1), metavar="N", help="Only the newest N tasks it fired.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("list_schedule_tasks")
@needs_client
def runs(client: FarmClient, schedule_id: str, limit: int | None, json_flag: bool) -> None:
    """List the tasks a schedule fired, newest first (`trigger` tells a cron firing from a `fire`)."""
    answer = client.request("GET", _path(schedule_id, "/tasks"), operation_id="list_schedule_tasks")
    if limit is not None:
        answer = {**answer, "items": answer["items"][:limit]}
    emit(answer, json_flag=json_flag, table=FIRED_COLUMNS)


# -- fire ---------------------------------------------------------------------------------------------------------


@schedule.command()
@_id_arg
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("fire_schedule")
@needs_client
def fire(client: FarmClient, schedule_id: str, json_flag: bool) -> None:
    """Launch the schedule's task now, once. The next cron firing is not moved and `--max-runs` does not count it."""
    fired = client.request("POST", _path(schedule_id, "/fire"), operation_id="fire_schedule")
    emit(fired, json_flag=json_flag, table=FIRED_COLUMNS)
    if not json_flag:
        _note(f"follow it with `dynfarm task watch {fired['task_id']}`")


# -- pause and resume ---------------------------------------------------------------------------------------------


@schedule.command()
@_id_arg
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("pause_schedule")
@needs_client
def pause(client: FarmClient, schedule_id: str, json_flag: bool) -> None:
    """Stop a schedule from firing until it is resumed. The occurrences missed meanwhile are counted, not replayed."""
    paused = client.request("POST", _path(schedule_id, "/pause"), operation_id="pause_schedule")
    emit(paused, json_flag=json_flag, table=SCHEDULE_COLUMNS)


@schedule.command()
@_id_arg
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("resume_schedule")
@needs_client
def resume(client: FarmClient, schedule_id: str, json_flag: bool) -> None:
    """Let a paused schedule fire again, from its next occurrence (never several at once to make up for the pause)."""
    resumed = client.request("POST", _path(schedule_id, "/resume"), operation_id="resume_schedule")
    emit(resumed, json_flag=json_flag, table=SCHEDULE_COLUMNS)


# -- delete -------------------------------------------------------------------------------------------------------


@schedule.command()
@_id_arg
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@operation("delete_schedule")
@needs_client
def delete(client: FarmClient, schedule_id: str, yes: bool) -> None:
    """Delete a schedule. Tasks it already fired keep running and stay listed under `task list`."""
    confirm(f"Delete schedule {schedule_id}?", yes=yes)
    client.request("DELETE", _path(schedule_id), operation_id="delete_schedule")
    click.echo(f"deleted schedule {schedule_id}")
