"""The installed package as a whole: the entry point, the version, every command's help, `--json` for scripts.

Why these tests exist: a wheel can be built and still be broken for a person: a command module missing from the
package, an entry point that points nowhere, a `--help` that raises because an option was declared twice, a `--json`
that prints a table. Command modules are imported lazily, so nothing but running each one finds the broken one. The
tree of commands is read from the code, so a new command is covered the day it is added.
"""

from __future__ import annotations

import click
import pytest
from fakefarm import ALICE

import dynfarm
from dynfarm.cli import COMMANDS, cli


def command_paths() -> list[tuple[str, ...]]:
    """Every command and group of the tree, as the words a person types, read from the code."""
    ctx = click.Context(cli)
    found: list[tuple[str, ...]] = []

    def visit(command: click.Command, path: tuple[str, ...]) -> None:
        if path:
            found.append(path)
        if isinstance(command, click.Group):
            for name in command.list_commands(ctx):
                child = command.get_command(ctx, name)
                assert child is not None, f"{(*path, name)} is listed but cannot be loaded"
                visit(child, (*path, name))

    visit(cli, ())
    return found


PATHS = command_paths()


def test_the_version_is_the_one_of_the_package(dynfarm_bin):
    run = dynfarm_bin.run("--version")
    assert run.code == 0 and run.stdout.strip() == f"dynfarm {dynfarm.__version__}", run.output


def test_the_top_level_help_lists_every_command(dynfarm_bin):
    run = dynfarm_bin.run("--help")
    assert run.code == 0, run.output
    for name in COMMANDS:
        assert f"  {name} " in run.stdout, f"{name} is missing from `dynfarm --help`"


def test_the_tree_has_every_group_of_the_cli():
    assert {path[0] for path in PATHS} == set(COMMANDS)
    assert len(PATHS) >= 35  # the groups and their commands: a lazy loader that lost a module would shrink this


@pytest.mark.parametrize("path", PATHS, ids=[" ".join(p) for p in PATHS])
def test_every_command_prints_its_help_from_the_installed_package(dynfarm_bin, path):
    run = dynfarm_bin.run(*path, "--help")
    assert run.code == 0, run.output
    assert run.stdout.startswith(f"Usage: dynfarm {' '.join(path)}"), run.stdout[:200]
    assert run.stderr == ""


@pytest.fixture
def seeded(logged_in, fake_farm):
    """One of each resource, made through the binary, and the commands that list or show them."""
    task_id = fake_farm.seed_task(ALICE, state="succeeded", logs=["one", "two"])
    schedule = logged_in.run(
        "schedule", "create", "--prompt", "hi", "--cron", "0 8 * * *", "--timezone", "UTC", "--json"
    ).json()["schedule_id"]
    assert logged_in.run("agent", "create", "mine", "--scope", "personal", "--instructions", "hi").code == 0
    assert logged_in.run("credential", "set", "key", "--type", "generic", input="not-a-real-secret\n").code == 0
    return {
        "whoami": ["whoami"],
        "plugin list": ["plugin", "list"],
        "callback-key": ["callback-key"],
        "agent list": ["agent", "list"],
        "agent view": ["agent", "view", "mine"],
        "task list": ["task", "list"],
        "task view": ["task", "view", task_id],
        "task logs": ["task", "logs", task_id],
        "task result": ["task", "result", task_id],
        "schedule list": ["schedule", "list"],
        "schedule view": ["schedule", "view", schedule],
        "schedule runs": ["schedule", "runs", schedule],
        "credential list": ["credential", "list"],
        "credential view": ["credential", "view", "key"],
    }


def test_every_read_command_prints_json_that_parses_when_asked(logged_in, seeded):
    for name, args in seeded.items():
        if name == "callback-key":  # a JWK as JSON already; it has no --json
            run = logged_in.run(*args)
        else:
            run = logged_in.run(*args, "--json")
        assert run.code == 0, (name, run.output)
        assert isinstance(run.json(), dict), name
        assert "Traceback" not in run.stderr


def test_a_read_command_in_a_pipe_prints_plain_rows_without_colour_codes(logged_in, seeded):
    run = logged_in.run(*seeded["task list"])
    assert run.code == 0 and "\x1b[" not in run.output
    assert run.stdout.count("\n") == 1 and run.stdout.split("\t")[2] == "succeeded"
