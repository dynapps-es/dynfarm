"""`dynfarm task`: launch a task, follow it to the end and read what it produced.

    task run [AGENT]   POST /tasks            task list    GET /tasks            task logs    GET /tasks/{id}/logs
    task view ID       GET  /tasks/{id}       task watch   GET /tasks/{id}?wait= task result  GET /tasks/{id}/result
    task cancel ID     DELETE /tasks/{id}

Where an ID is expected, `last` is the newest task *you* launched (`GET /tasks?owner=me&limit=1`): `owner=me` matters
to a tenant admin, who otherwise sees everyone's and would get somebody else's task.

Choices worth knowing:

- **`run` prints the id (and state) like a listing row, hints on stderr.** Scripts take the id with `cut -f1`, or use
  `--json`. It tells when the farm answered `200` instead of `201`: the `--correlation-id` was already used, so no
  second task was created (`farm-api.md` §7.1). It warns once that the farm stores the prompt and the input.
- **`watch` waits on the farm, not by polling** (`GET /tasks/{id}?wait=30`, `farm-api.md` §7.3): one request per 30 s
  while the task is not finished. Progress lines go to stderr; stdout gets the final task, as `view` prints it (or as
  JSON), so `watch --json | jq` works. If the farm answers at once without a terminal state (a proxy or an older farm
  ignoring `wait`), the loop pauses 2 s instead of spinning. Ctrl-C stops *watching*, not the task (exit 130).
- **Exit codes**: a finished task is exit 0 whatever its state, unless `--exit-status` asks for 1 when it did not
  `succeed`. `run --exit-status` implies `--watch`.
- **`result --download` leaves the farm**: the result link is a presigned URL on the object store, fetched with a client
  that has no `Authorization` header, so the farm's token never reaches another host. The file is checked against the
  `sha256` the farm gave, written beside its destination and renamed into place, and an existing file is not replaced
  without `--force`.
- Everything from the farm that a person or an agent wrote (prompt, input, log lines, the summary) is cleaned of
  control characters before it reaches the terminal.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click

from dynfarm.commands import confirm
from dynfarm.config import is_loopback
from dynfarm.context import AppContext
from dynfarm.errors import ConfigError, DynfarmError, FarmError
from dynfarm.http import FarmClient, needs_client, segment
from dynfarm.output import Column, clean_text, emit
from dynfarm.registry import operation
from dynfarm.taskbody import (
    STORED_WARNING,
    TERMINAL_STATES,
    TaskFlags,
    build_task_body,
    stores_text,
    task_body_options,
)

JSON_HELP = "Print the farm's answer as JSON."

#: Seconds a `watch` request is held open by the farm (the API's maximum), and the pause when it answers at once.
WAIT_SECONDS = 30
QUICK_ANSWER = 1.0
QUICK_PAUSE = 2.0

INTERRUPTED = 130

TASK_COLUMNS = (
    Column("id", "task_id"),
    Column("agent", "agent_id"),
    Column("state", "state"),
    Column("created", "created_at", "date"),
    Column("owner", "owner"),
)

CREATED_COLUMNS = (
    Column("id", "task_id"),
    Column("state", "state"),
    Column("scope", "scope"),
    Column("correlation id", "correlation_id"),
)


@click.group()
def task() -> None:
    """Launch tasks on the farm, follow them and read their results."""


# -- shared pieces ------------------------------------------------------------------------------------------------


def resolve_task_id(client: FarmClient, task_id: str) -> str:
    """The id to use: `last` becomes the newest task of the calling key, anything else is passed on as it is."""
    if task_id != "last":
        return task_id
    page = client.request("GET", "/tasks", params={"owner": "me", "limit": 1}, operation_id="list_tasks")
    if not page["items"]:
        raise DynfarmError("you have no tasks yet, so there is no `last`")
    return page["items"][0]["task_id"]


def _path(task_id: str, suffix: str = "") -> str:
    return f"/tasks/{segment(task_id)}{suffix}"


def _task_arg(command: Any) -> Any:
    return click.argument("task_id", metavar="ID|last")(command)


def _note(message: str) -> None:
    click.echo(message, err=True)


def _state_line(task_data: dict[str, Any]) -> str:
    """`running`, `queued (position 2)`, `failed (timeout)`: a state and the one detail that explains it."""
    state = str(task_data.get("state"))
    if state == "failed" and task_data.get("failure_reason"):
        return f"{state} ({task_data['failure_reason']})"
    if state == "queued" and task_data.get("queue_position") is not None:
        return f"{state} (position {task_data['queue_position']})"
    return state


def _fields(task_data: dict[str, Any]) -> list[tuple[str, Any]]:
    usage = task_data.get("usage") or {}
    callback = task_data.get("callback") or {}
    workspace = task_data.get("workspace") or {}
    return [
        ("id", task_data.get("task_id")),
        ("state", _state_line(task_data)),
        ("agent", task_data.get("agent_id") or ("(free prompt)" if task_data.get("prompt") else None)),
        ("scope", task_data.get("scope")),
        ("owner", task_data.get("owner")),
        ("created", task_data.get("created_at")),
        ("started", task_data.get("started_at")),
        ("finished", task_data.get("finished_at")),
        ("duration", f"{usage['duration_s']} s" if usage.get("duration_s") is not None else None),
        ("cost", f"{usage['cost_eur']} EUR" if usage.get("cost_eur") is not None else None),
        ("correlation id", task_data.get("correlation_id")),
        ("session", task_data.get("session_ref")),
        ("repository", workspace.get("repo")),
        ("callback", callback.get("status")),
    ]


def _print_task(task_data: dict[str, Any], *, json_flag: bool) -> None:
    """The task as labelled lines (a prompt and an input as indented blocks after them), or as the farm's JSON."""
    if json_flag:
        emit(task_data, json_flag=True)
        return
    rows = [(label, value) for label, value in _fields(task_data) if value not in (None, "")]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        click.echo(f"{label + ':':<{width}} {clean_text(str(value))}")
    for label in ("prompt", "input"):
        if task_data.get(label):
            click.echo(f"{label}:")
            for line in clean_text(str(task_data[label]), multiline=True).splitlines():
                click.echo(f"  {line}")


def _follow(client: FarmClient, task_id: str) -> dict[str, Any]:
    """Hold `GET /tasks/{id}?wait=30` until the task is terminal, noting each change of state on stderr; return it."""
    shown: str | None = None
    while True:
        started = time.monotonic()
        current = client.request("GET", _path(task_id), params={"wait": WAIT_SECONDS}, operation_id="get_task")
        line = _state_line(current)
        if line != shown:
            _note(line)
            shown = line
        if current.get("state") in TERMINAL_STATES:
            return current
        if time.monotonic() - started < QUICK_ANSWER:
            client.pause(QUICK_PAUSE)  # the farm did not hold the request: do not spin


def _watch(client: FarmClient, task_id: str, *, exit_status: bool, json_flag: bool) -> None:
    try:
        finished = _follow(client, task_id)
    except KeyboardInterrupt:
        _note(f"\nstopped watching; the task keeps running — `dynfarm task watch {task_id}` follows it again")
        raise click.exceptions.Exit(INTERRUPTED) from None
    _print_task(finished, json_flag=json_flag)
    if exit_status and finished.get("state") != "succeeded":
        raise click.exceptions.Exit(1)


# -- run ----------------------------------------------------------------------------------------------------------


@task.command()
@click.argument("agent", required=False)
@task_body_options
@click.option("--watch", is_flag=True, help="Follow the task until it finishes, as `task watch` does.")
@click.option("--exit-status", is_flag=True, help="Exit 1 unless the task succeeds. Implies --watch.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("create_task", "get_task")
@needs_client
def run(
    client: FarmClient,
    agent: str | None,
    task_flags: TaskFlags,
    watch: bool,
    exit_status: bool,
    json_flag: bool,
) -> None:
    """Launch a task: an AGENT with --input, or a free --prompt.

    \b
    dynfarm task run developer-skills:code-reviewer --input "Review MR 42 of group/project."
    dynfarm task run --prompt "Summarise the open TODOs." --scope docs --watch
    dynfarm task run --body-file task.json --exit-status      # workspace, limits... have no flag
    """
    body = build_task_body(task_flags, agent=agent)
    if stores_text(body):
        _note(STORED_WARNING)
    created = client.request("POST", "/tasks", json=body, operation_id="create_task")
    following = watch or exit_status
    if client.last_reply is not None and client.last_reply.status == 200:
        _note("the farm already had a task with this correlation id and returned it; no new task was created")
    if not (following and json_flag):
        emit(created, json_flag=json_flag, table=CREATED_COLUMNS)
    if following:
        _watch(client, created["task_id"], exit_status=exit_status, json_flag=json_flag)
    elif not json_flag:
        _note(f"follow it with `dynfarm task watch {created['task_id']}`")


# -- list ---------------------------------------------------------------------------------------------------------


@task.command("list")
@click.option("--state", "states", multiple=True, metavar="STATE", help="Only this state (repeatable).")
@click.option("--agent", "agent_id", metavar="AGENT", help="Only tasks of this agent.")
@click.option("--scope", metavar="SLUG", help="Only tasks launched with this scope label.")
@click.option("--owner", metavar="OWNER", help="`me`, or (tenant admins) a person's subject.")
@click.option("--since", metavar="TIME", help="Only tasks created at or after this ISO 8601 time.")
@click.option("--until", metavar="TIME", help="Only tasks created before this ISO 8601 time.")
@click.option("--limit", type=click.IntRange(1, 100), help="Page size (the farm's default is 25, at most 100).")
@click.option("--cursor", metavar="C", help="Start at this page: the next_cursor of the previous one.")
@click.option("--all", "every_page", is_flag=True, help="Follow next_cursor and list every page.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("list_tasks")
@needs_client
def list_(
    client: FarmClient,
    states: tuple[str, ...],
    agent_id: str | None,
    scope: str | None,
    owner: str | None,
    since: str | None,
    until: str | None,
    limit: int | None,
    cursor: str | None,
    every_page: bool,
    json_flag: bool,
) -> None:
    """List tasks, newest first."""
    params = {
        "state": ",".join(states) or None,
        "agent_id": agent_id,
        "scope": scope,
        "owner": owner,
        "since": since,
        "until": until,
        "limit": limit,
        "cursor": cursor,
    }
    params = {key: value for key, value in params.items() if value is not None}
    if every_page:
        page = client.pages("/tasks", params=params, operation_id="list_tasks")
    else:
        page = client.request("GET", "/tasks", params=params, operation_id="list_tasks")
    emit(page, json_flag=json_flag, table=TASK_COLUMNS)
    if not every_page and not json_flag and page.get("next_cursor"):
        _note(f"more tasks: add --cursor {page['next_cursor']} (or --all)")


# -- view ---------------------------------------------------------------------------------------------------------


@task.command()
@_task_arg
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("get_task", "list_tasks")
@needs_client
def view(client: FarmClient, task_id: str, json_flag: bool) -> None:
    """Show a task: its state, who ran it, what it was asked and what it cost."""
    task_id = resolve_task_id(client, task_id)
    _print_task(client.request("GET", _path(task_id), operation_id="get_task"), json_flag=json_flag)


# -- watch --------------------------------------------------------------------------------------------------------


@task.command()
@_task_arg
@click.option("--exit-status", is_flag=True, help="Exit 1 unless the task succeeded.")
@click.option("--json", "json_flag", is_flag=True, help="At the end, print the finished task as the farm's JSON.")
@operation("get_task", "list_tasks")
@needs_client
def watch(client: FarmClient, task_id: str, exit_status: bool, json_flag: bool) -> None:
    """Wait for a task to finish, printing each change of state (Ctrl-C stops watching, not the task)."""
    task_id = resolve_task_id(client, task_id)
    _watch(client, task_id, exit_status=exit_status, json_flag=json_flag)


# -- logs ---------------------------------------------------------------------------------------------------------


@task.command()
@_task_arg
@click.option("--tail", type=click.IntRange(1, 1000), metavar="N", help="How many of the last lines (default 200).")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("get_logs", "list_tasks")
@needs_client
def logs(client: FarmClient, task_id: str, tail: int | None, json_flag: bool) -> None:
    """Print the last lines of a task's log; once it has finished, the link to the complete log."""
    task_id = resolve_task_id(client, task_id)
    answer = client.request("GET", _path(task_id, "/logs"), params={"tail_lines": tail}, operation_id="get_logs")
    if json_flag:
        emit(answer, json_flag=True)
        return
    for line in answer["lines"]:
        click.echo(clean_text(line, multiline=True))
    if not answer["lines"]:
        _note(f"no log lines yet (the task is {answer['state']})")
    full = answer.get("full_logs")
    if full:
        _note(f"complete log: {clean_text(full['url'])} (valid until {full['expires_at']})")


# -- result -------------------------------------------------------------------------------------------------------


@task.command()
@_task_arg
@click.option(
    "--download", "destination", type=click.Path(dir_okay=False), metavar="PATH", help="Save the result file."
)
@click.option("--force", is_flag=True, help="With --download, replace the file if it exists.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("get_result", "list_tasks")
@needs_client
def result(client: FarmClient, task_id: str, destination: str | None, force: bool, json_flag: bool) -> None:
    """Show what a finished task produced: its summary, usage and the link to the full result."""
    task_id = resolve_task_id(client, task_id)
    target = Path(destination) if destination else None
    if target is not None:
        _check_destination(target, force=force)
    answer = client.request("GET", _path(task_id, "/result"), operation_id="get_result")
    if target is not None:
        app = click.get_current_context().ensure_object(AppContext)
        written = _download(answer["result"], target, transport=app.transport, force=force)
        _note(f"saved {written} bytes to {target}")
    if json_flag:
        emit(answer, json_flag=True)
        return
    _print_result(answer)


def _print_result(answer: dict[str, Any]) -> None:
    link, usage = answer["result"], answer.get("usage") or {}
    rows = [
        ("state", answer.get("state")),
        ("duration", f"{usage['duration_s']} s" if usage.get("duration_s") is not None else None),
        ("cost", f"{usage['cost_eur']} EUR" if usage.get("cost_eur") is not None else None),
        ("tokens", _tokens(usage)),
        ("session", answer.get("session_ref")),
        ("resumable", {True: "yes", False: "no"}.get(answer.get("resumable"))),
        ("size", f"{link['size_bytes']} bytes" if link.get("size_bytes") is not None else None),
        ("sha256", link.get("sha256")),
        ("result", link.get("url")),
        ("link valid until", link.get("expires_at")),
    ]
    rows = [(label, value) for label, value in rows if value not in (None, "")]
    width = max(len(label) for label, _ in rows) + 1
    if answer.get("summary"):
        click.echo("summary:")
        for line in clean_text(answer["summary"], multiline=True).splitlines():
            click.echo(f"  {line}")
    for label, value in rows:
        click.echo(f"{label + ':':<{width}} {clean_text(str(value))}")


def _tokens(usage: dict[str, Any]) -> str | None:
    if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
        return None
    return f"{usage.get('input_tokens') or 0} in, {usage.get('output_tokens') or 0} out"


def _check_destination(target: Path, *, force: bool) -> None:
    """Refuse before asking the farm anything: an existing file without --force, or a directory that is not there."""
    if target.exists() and not force:
        raise ConfigError(f"{target} exists; pass --force to replace it")
    if not target.parent.is_dir():
        raise ConfigError(f"the directory {target.parent} does not exist")


def _download(link: dict[str, Any], target: Path, *, transport: Any, force: bool) -> int:
    """Fetch the presigned result URL into `target`; return the bytes written.

    The request carries no `Authorization` (nor any token): the URL is on the object store, not on the farm, and its
    own signature is the credential. Only `https` is followed (or loopback, for the tests and a local farm).
    """
    import httpx

    url = str(link.get("url") or "")
    parts = urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback(f"http://{parts.netloc}")):
        raise FarmError(200, "response_invalid", "the result link is not an https address; refusing to download it")
    check = hashlib.sha256()
    written = 0
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with (
            os.fdopen(handle, "wb") as out,
            httpx.Client(transport=transport, timeout=httpx.Timeout(60.0, connect=10.0)) as plain,
            plain.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                # Say where, never the URL: its query string is the signature.
                raise DynfarmError(f"the download from {parts.netloc} failed: HTTP {response.status_code}")
            for chunk in response.iter_bytes():
                out.write(chunk)
                check.update(chunk)
                written += len(chunk)
        expected = link.get("sha256")
        if expected and check.hexdigest() != expected:
            raise DynfarmError("the downloaded file does not match the sha256 the farm gave; it was not saved")
        if target.exists() and not force:  # appeared while downloading
            raise ConfigError(f"{target} exists; pass --force to replace it")
        os.replace(temporary, target)
    except httpx.HTTPError as exc:
        raise DynfarmError(f"the download from {parts.netloc} failed: {exc.__class__.__name__}") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return written


# -- cancel -------------------------------------------------------------------------------------------------------


@task.command()
@_task_arg
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("cancel_task", "list_tasks")
@needs_client
def cancel(client: FarmClient, task_id: str, yes: bool, json_flag: bool) -> None:
    """Cancel a task that has not finished. Its pod is deleted; the work done so far is lost."""
    task_id = resolve_task_id(client, task_id)
    confirm(f"Cancel task {task_id}?", yes=yes)
    answer = client.request("DELETE", _path(task_id), operation_id="cancel_task")
    if json_flag:
        emit(answer, json_flag=True)
    else:
        click.echo(f"cancelled task {answer['task_id']}")
