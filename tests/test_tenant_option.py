"""`--tenant` on the read commands: how a key with `farm:platform:read` looks at another tenant.

Why these tests exist: the API takes `tenant` on every GET, and only such a key may use it (anyone else is `403
insufficient_scope`). The option has to be on every command that reads, has to go out as a query parameter of the GET
that reads and of no other request, and must not exist on a command that changes something, where it would suggest
that a write could cross tenants.
"""

from __future__ import annotations

import click
import pytest
from fakefarm import ALICE, PLATFORM

from dynfarm import registry
from dynfarm.cli import cli

READS = [
    (["whoami"], "whoami"),
    (["plugin", "list"], "list_plugins"),
    (["callback-key"], "notify_key"),
    (["task", "list"], "list_tasks"),
    (["agent", "list"], "list_agents"),
    (["agent", "view", "example-plugin:code-reviewer"], "get_agent"),
    (["schedule", "list"], "list_schedules"),
    (["credential", "list"], "list_credentials"),
]


def sent(fake_farm, operation_id):
    return [r for r in fake_farm.requests if r.operation_id == operation_id]


@pytest.mark.parametrize(("args", "operation_id"), READS, ids=[" ".join(a) for a, _ in READS])
def test_a_read_command_sends_the_tenant_it_was_given(run_cli, fake_farm, args, operation_id):
    run_cli([*args, "--tenant", "tenant-a"], token=PLATFORM)
    assert sent(fake_farm, operation_id)[0].query["tenant"] == ["tenant-a"]


@pytest.mark.parametrize(("args", "operation_id"), READS, ids=[" ".join(a) for a, _ in READS])
def test_without_the_option_no_tenant_is_sent(run_cli, fake_farm, args, operation_id):
    run_cli(args)
    assert "tenant" not in sent(fake_farm, operation_id)[0].query


@pytest.mark.parametrize(("args", "operation_id"), READS, ids=[" ".join(a) for a, _ in READS])
def test_another_tenant_needs_the_platform_scope(run_cli, args, operation_id):
    result = run_cli([*args, "--tenant", "acme"], token=ALICE)
    assert result.exit_code == 1 and "insufficient_scope" in result.output


def test_the_listings_take_every_tenant_and_the_single_reads_do_not_say_so(run_cli):
    assert "SLUG|*" in run_cli(["task", "list", "--help"]).stdout
    assert "SLUG|*" not in run_cli(["task", "view", "--help"]).stdout
    assert "--tenant" in run_cli(["task", "view", "--help"]).stdout


def test_task_view_logs_result_and_transcript_read_the_task_in_that_tenant(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    for command, operation_id in (
        ("view", "get_task"),
        ("logs", "get_logs"),
        ("result", "get_result"),
        ("transcript", "get_transcript"),
    ):
        run_cli(["task", command, task_id, "--tenant", "tenant-a"], token=PLATFORM)
        assert sent(fake_farm, operation_id)[0].query["tenant"] == ["tenant-a"], command


def test_a_command_that_changes_something_has_no_tenant_option():
    registry.load_all_commands()
    writers = {
        ("task", "run"), ("task", "cancel"), ("task", "answer"), ("agent", "create"), ("agent", "edit"),
        ("agent", "delete"), ("schedule", "create"), ("schedule", "edit"), ("schedule", "fire"),
        ("schedule", "pause"), ("schedule", "resume"), ("schedule", "delete"), ("credential", "set"),
        ("credential", "upload-link"), ("credential", "delete"), ("person", "offboard"), ("person", "readmit"),
    }  # fmt: skip
    ctx = click.Context(cli)
    for group_name, name in sorted(writers):
        group = cli.get_command(ctx, group_name)
        command = group.get_command(click.Context(group, parent=ctx), name)
        assert "tenant" not in [p.name for p in command.params], (group_name, name)
