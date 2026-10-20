"""Building the body of a task (`TaskCreate`) from command-line flags and an optional JSON file.

Two commands describe a task the same way: `task run` launches one now, and a schedule carries one to launch later
(its `task` is "exactly the body of `POST /tasks`", `farm-api.md` §7.8). They share these flags and this builder, so a
task written for one can be written for the other:

    @click.command()
    @click.argument("agent", required=False)
    @task_body_options
    def run(agent, task_flags): ...
        body = build_task_body(task_flags, agent=agent)

What the flags are, and why they are only the common part of the body:

- `AGENT` (the `agent_id`), `--prompt`/`--prompt-file`, `--input`/`--input-file`, `--scope` (the person's own label,
  a lowercase slug), `--correlation-id` (the idempotency key) and `--session-ref` (resume a conversation) cover what
  most tasks need.
- Everything else `TaskCreate` knows (`workspace`, `marketplace`, `mcp_servers`, `limits`, `callback`, `image`,
  `services`, ...) is nested or rare, and a flag per field would be a second copy of the OpenAPI that drifts. It goes
  in `--body-file FILE` (`-` for standard input): a complete `TaskCreate` JSON object. A flag overrides the same field
  of the file.
- A prompt or an input is text, so it can be long and multi-line: it comes from a flag or from a file (`-` is standard
  input), never from a prompt of ours. At most one thing reads standard input.

The builder does not duplicate the farm's validation (lengths, patterns, which tenant may use a free prompt: the farm
answers `422` with a code and this tool shows it). It only refuses what cannot be a task at all, before any request:
both an agent and a prompt, neither, text given twice, an unreadable or non-object file. Error messages name flags and
fields, never the text itself: a prompt may hold what the person did not mean to show.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import click

from dynfarm.errors import ConfigError
from dynfarm.inputs import read_json_object, read_text

F = TypeVar("F", bound=Callable[..., Any])

#: The states from which a task never moves again (`farm-api.md` §6).
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "expired")

STORED_WARNING = (
    "note: the farm stores the prompt and the input of a task, and they can end up in its logs — keep secrets out"
)


@dataclass(frozen=True)
class TaskFlags:
    """The task flags as the person gave them, before any file is read: `build_task_body` turns them into a body."""

    prompt: str | None = None
    prompt_file: str | None = None
    input_text: str | None = None
    input_file: str | None = None
    scope: str | None = None
    correlation_id: str | None = None
    session_ref: str | None = None
    body_file: str | None = None


def task_body_options(fn: F | None = None, *, correlation_id: bool = True) -> Any:
    """Add the task flags to a command and hand them to the callback as one `task_flags: TaskFlags` argument.

    `correlation_id=False` leaves `--correlation-id` out: a schedule's task must not carry one (each firing generates
    its own).
    """

    def decorate(callback: F) -> F:
        @functools.wraps(callback)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            flags = TaskFlags(
                prompt=kwargs.pop("prompt", None),
                prompt_file=kwargs.pop("prompt_file", None),
                input_text=kwargs.pop("input_text", None),
                input_file=kwargs.pop("input_file", None),
                scope=kwargs.pop("scope", None),
                correlation_id=kwargs.pop("correlation_id", None),
                session_ref=kwargs.pop("session_ref", None),
                body_file=kwargs.pop("body_file", None),
            )
            return callback(*args, task_flags=flags, **kwargs)

        options = [
            click.option("--prompt", metavar="TEXT", help="A free-form instruction, instead of an AGENT."),
            click.option("--prompt-file", metavar="FILE", help="Read the prompt from a file ('-' for standard input)."),
            click.option(
                "--input",
                "input_text",
                metavar="TEXT",
                help="What the agent has to work on (required by an AGENT, optional with --prompt).",
            ),
            click.option("--input-file", metavar="FILE", help="Read the input from a file ('-' for standard input)."),
            click.option("--scope", metavar="SLUG", help="Your own label to group tasks (a lowercase slug)."),
            click.option(
                "--session-ref", metavar="REF", help="Continue a conversation: the session_ref of a finished task."
            ),
            click.option(
                "--body-file",
                metavar="FILE",
                help="A complete task as JSON ('-' for standard input), for what has no flag (workspace, "
                "marketplace, mcp_servers, limits, callback, image...). The flags override its fields.",
            ),
        ]
        if correlation_id:
            options.append(
                click.option(
                    "--correlation-id",
                    metavar="ID",
                    help="Your idempotency key: repeating it returns the existing task instead of a second one.",
                )
            )
        for option in reversed(options):
            wrapper = option(wrapper)
        return wrapper  # type: ignore[return-value]

    return decorate(fn) if fn is not None else decorate


def build_task_body(flags: TaskFlags, *, agent: str | None = None) -> dict[str, Any]:
    """The `TaskCreate` body for these flags (and the positional `agent`, when the command has one).

    The body file is the base, each flag replaces its field. Raises `ConfigError` (exit 2) when the result cannot be a
    task: it names both an agent and a prompt, or neither.
    """
    readers = [
        name
        for name, value in (
            ("--prompt-file", flags.prompt_file),
            ("--input-file", flags.input_file),
            ("--body-file", flags.body_file),
        )
        if value == "-"
    ]
    if len(readers) > 1:
        raise ConfigError(f"only one of {', '.join(readers)} can read standard input")

    body = read_json_object(flags.body_file, "--body-file", "a task") if flags.body_file else {}
    if agent is not None:
        body["agent_id"] = agent
    for field, text, file, flag, file_flag in (
        ("prompt", flags.prompt, flags.prompt_file, "--prompt", "--prompt-file"),
        ("input", flags.input_text, flags.input_file, "--input", "--input-file"),
    ):
        if text is not None and file is not None:
            raise ConfigError(f"give {flag} or {file_flag}, not both")
        if file is not None:
            text = read_text(file, file_flag)
        if text is not None:
            if not text.strip():
                raise ConfigError(f"the {field} is empty")
            body[field] = text
    for field, value in (
        ("scope", flags.scope),
        ("correlation_id", flags.correlation_id),
        ("session_ref", flags.session_ref),
    ):
        if value is not None:
            body[field] = value

    has_agent, has_prompt = bool(body.get("agent_id")), bool(body.get("prompt"))
    if has_agent and has_prompt:
        raise ConfigError("a task runs an AGENT or a --prompt, not both (check --body-file too)")
    if not has_agent and not has_prompt:
        raise ConfigError("say what to run: an AGENT, or --prompt / --prompt-file")
    return body


def stores_text(body: dict[str, Any]) -> bool:
    """Whether the body sends text the farm will keep (a prompt or an input), so the person is told once."""
    return bool(body.get("prompt") or body.get("input"))
