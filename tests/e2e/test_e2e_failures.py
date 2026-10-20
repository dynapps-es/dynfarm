"""What the shell sees when something goes wrong: exit codes and messages from the installed binary over a socket.

Why these tests exist: scripts and CI jobs branch on `$?` (0 ok, 1 failure, 2 usage or configuration, 4 authentication
needed) and people read stderr. In-process tests see exceptions; only a subprocess sees the exit code the shell gets,
that a traceback never reaches a person, that stdout stays clean for a pipe, and that the retry pauses are real
seconds. Every case here is one the farm really produces: a missing, wrong or expired token (401), someone else's
object (404, not 403), an invalid value (422), a missing scope (403), a full quota (429 with `Retry-After`).
"""

from __future__ import annotations

import socket

from fakefarm import ALICE, BOB, EXPIRED, OTHER, READONLY


def no_traceback(run) -> None:
    assert "Traceback" not in run.output, run.output


def test_no_token_exits_4_and_says_how_to_log_in(dynfarm, fake_farm):
    run = dynfarm.run("whoami")
    assert run.code == 4 and run.stdout == "", run.output
    assert "dynfarm auth login" in run.stderr
    assert fake_farm.requests == []  # nothing is sent without a token
    no_traceback(run)


def test_a_token_the_farm_does_not_know_exits_4_with_its_code(dynfarm):
    run = dynfarm.run("whoami", env={"DYNFARM_TOKEN": "tok-unknown"})
    assert run.code == 4 and run.stdout == "", run.output
    assert "token_invalid" in run.stderr
    no_traceback(run)


def test_an_expired_token_exits_4_and_is_told_apart_from_an_invalid_one(dynfarm):
    run = dynfarm.run("task", "list", env={"DYNFARM_TOKEN": EXPIRED})
    assert run.code == 4, run.output
    assert "token_expired" in run.stderr and "token_invalid" not in run.stderr
    no_traceback(run)


def test_someone_elses_task_is_a_404_for_another_person_and_for_another_tenant(dynfarm, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    for token in (BOB, OTHER):
        run = dynfarm.run("task", "view", task_id, env={"DYNFARM_TOKEN": token})
        assert run.code == 1 and "not_found" in run.stderr, run.output
        assert "403" not in run.stderr and "forbidden" not in run.stderr.lower()
        assert dynfarm.run("task", "list", "--json", env={"DYNFARM_TOKEN": token}).json()["items"] == []
    assert dynfarm.run("task", "view", task_id, env={"DYNFARM_TOKEN": ALICE}).code == 0


def test_a_missing_scope_is_a_failure_with_the_farms_code(dynfarm, fake_farm):
    run = dynfarm.run("task", "run", "--prompt", "hi", env={"DYNFARM_TOKEN": READONLY})
    assert run.code == 1 and "insufficient_scope" in run.stderr, run.output
    no_traceback(run)


def test_a_value_the_farm_refuses_is_a_failure_with_its_message_and_code(dynfarm):
    run = dynfarm.run("task", "list", "--state", "bogus", env={"DYNFARM_TOKEN": ALICE})
    assert run.code == 1 and "state_invalid" in run.stderr, run.output
    assert run.stdout == ""


def test_a_usage_mistake_exits_2_and_sends_nothing(dynfarm, fake_farm):
    for args in (["task", "list", "--bogus"], ["credential", "set", "x"], ["task", "view"], ["nonsense"]):
        run = dynfarm.run(*args, env={"DYNFARM_TOKEN": ALICE})
        assert run.code == 2, (args, run.output)
    assert fake_farm.requests == []


def test_a_secret_is_never_accepted_on_the_command_line(dynfarm, fake_farm):
    run = dynfarm.run(
        "credential", "set", "x", "--type", "generic", "--value", "sk-not-a-real-secret", env={"DYNFARM_TOKEN": ALICE}
    )
    assert run.code == 2 and "sk-not-a-real-secret" not in run.output
    assert fake_farm.requests == []


def test_a_read_that_hits_a_full_quota_waits_the_retry_after_and_succeeds(dynfarm, fake_farm):
    fake_farm.fail_next(
        "list_tasks", status=429, code="quota_exceeded", message="slow down", retryable=True, retry_after=1
    )
    run = dynfarm.run("task", "list", "--json", env={"DYNFARM_TOKEN": ALICE})
    assert run.code == 0, run.output
    assert [r.status for r in fake_farm.requests] == [429, 200]
    assert run.seconds >= 1.0  # the pause was real
    assert run.json()["items"] == []


def test_a_launch_that_hits_a_full_quota_is_not_repeated_and_says_when_to_try_again(dynfarm, fake_farm):
    fake_farm.fail_next(
        "create_task", status=429, code="quota_exceeded", message="slow down", retryable=True, retry_after=1
    )
    run = dynfarm.run("task", "run", "--prompt", "hi", env={"DYNFARM_TOKEN": ALICE})
    assert run.code == 1, run.output
    assert [r.status for r in fake_farm.requests] == [429]  # a POST never repeats by itself: it could launch twice
    assert "quota_exceeded" in run.stderr and "try again in 1 s" in run.stderr
    no_traceback(run)


def test_a_token_is_never_sent_over_plain_http_to_a_remote_host(dynfarm, fake_farm):
    run = dynfarm.run("whoami", "--hostname", "http://farm.example.com", env={"DYNFARM_TOKEN": ALICE})
    assert run.code == 2 and "https" in run.stderr, run.output
    assert ALICE not in run.output and fake_farm.requests == []


def test_a_farm_that_is_not_there_is_a_failure_not_a_traceback(dynfarm):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    run = dynfarm.run("whoami", env={"DYNFARM_TOKEN": ALICE, "DYNFARM_HOST": f"http://127.0.0.1:{closed}"})
    assert run.code == 1, run.output
    assert run.stdout == "" and run.stderr.startswith("error:"), run.stderr
    no_traceback(run)


def test_destructive_commands_without_a_terminal_and_without_yes_refuse_and_send_nothing(logged_in, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    for args in (
        ["task", "cancel", task_id],
        ["agent", "delete", "repo-helper"],
        ["schedule", "delete", "sch-1"],
        ["credential", "delete", "x"],
        ["auth", "logout"],
    ):
        run = logged_in.run(*args)
        assert run.code == 2 and "--yes" in run.stderr, (args, run.output)
    assert fake_farm.requests == []
    assert logged_in.run("whoami").code == 0  # and the refused logout kept the token
