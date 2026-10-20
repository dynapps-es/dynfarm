"""`dynfarm schedule`: recurring tasks, created and operated by a person or a pipeline.

Why these tests exist: a schedule launches tasks on the farm's own clock, long after the command that created it has
returned and with nobody watching, so what `create` sends has to be exactly what the flags and files describe (the
task through the same builder as `task run`, the recurrence with a timezone somebody chose, never a guessed one). The
farm polices the cron, the zone and the schedule's life cycle (`cron_invalid`, `timezone_invalid`, `schedule_finished`,
`schedule_not_active`, `schedule_overlap`) and the commands must show those answers with their codes, not turn them
into a crash or a retry. A schedule belongs to its owner, so another person's or tenant's is a 404, and `delete` must
ask first. The fake farm validates every request against the pinned OpenAPI, so a field `ScheduleCreate` or
`RecurrenceIn` does not know fails here even when a test does not look.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fakefarm import ADMIN, ALICE, BOB, OTHER, READONLY

import dynfarm.commands
import dynfarm.output as output
from dynfarm import registry

API = "/farm/api/v1/schedules"
REVIEWER = "developer-skills:code-reviewer"
WEEKDAYS = "0 8 * * 1-5"
MADRID = "Europe/Madrid"


def body_of(request) -> dict:
    return json.loads(request.body)


def sent(fake_farm, operation_id):
    return [r for r in fake_farm.requests if r.operation_id == operation_id]


def printed(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def rows(result) -> list[list[str]]:
    """The tab-separated rows a listing prints when stdout is a pipe."""
    assert result.exit_code == 0, result.output
    return [line.split("\t") for line in result.stdout.splitlines()]


def make(fake_farm, token=ALICE, *, task=None, recurrence=None, **fields) -> str:
    """Arrange: create a schedule through the API as `token`, without leaving the call in `fake_farm.requests`."""
    body = {
        "task": task or {"agent_id": REVIEWER, "input": "Review my open MRs."},
        "recurrence": recurrence or {"cron": WEEKDAYS, "timezone": MADRID},
        **fields,
    }
    with httpx.Client(transport=fake_farm.transport(), headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.post(fake_farm.base_url + API, json=body)
    assert response.status_code == 201, response.text
    fake_farm.requests.clear()
    return response.json()["schedule_id"]


@pytest.fixture
def terminal(monkeypatch):
    """`terminal(True)`: a person is at a terminal (so `delete` may ask), `terminal(False)`: a script."""

    def choose(tty):
        monkeypatch.setattr(dynfarm.commands, "is_interactive", lambda: tty)

    return choose


def create_args(*extra: str) -> list[str]:
    return ["schedule", "create", *extra]


# -- create: the task and the recurrence --------------------------------------------------------------------------


def test_create_from_flags_sends_the_task_and_the_recurrence_and_nothing_the_person_did_not_ask_for(run_cli, fake_farm):
    result = run_cli(
        create_args(REVIEWER, "--input", "Review my open MRs.", "--cron", WEEKDAYS, "--timezone", MADRID, "--json")
    )
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_schedule")[0]) == {
        "task": {"agent_id": REVIEWER, "input": "Review my open MRs."},
        "recurrence": {"cron": WEEKDAYS, "timezone": MADRID},
    }
    created = json.loads(result.stdout)
    assert created["state"] == "active" and created["owner"] == "alice" and created["overlap"] == "skip"


def test_create_prints_the_id_state_and_cron_as_a_row_and_hints_how_to_pause(run_cli, fake_farm):
    result = run_cli(create_args(REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID))
    [row] = rows(result)
    schedule_id = next(iter(fake_farm._schedules))
    assert row[:4] == [schedule_id, "active", WEEKDAYS, MADRID]
    assert f"dynfarm schedule pause {schedule_id}" in result.stderr


def test_create_a_free_prompt_schedule_reads_the_prompt_from_a_file(run_cli, fake_farm, tmp_path):
    prompt = tmp_path / "digest.txt"
    prompt.write_text("Summarise the week.\nBe brief.\n")
    result = run_cli(
        create_args("--prompt-file", str(prompt), "--scope", "docs", "--cron", "0 7 * * 1", "--timezone", "UTC")
    )
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_schedule")[0])["task"] == {
        "prompt": "Summarise the week.\nBe brief.",
        "scope": "docs",
    }
    assert rows(result)[0][6] == "(free prompt)"


def test_create_takes_the_task_from_a_body_file_and_the_recurrence_from_a_recurrence_file(run_cli, fake_farm, tmp_path):
    task_file, recurrence_file = tmp_path / "task.json", tmp_path / "recurrence.json"
    task_file.write_text(json.dumps({"agent_id": REVIEWER, "input": "Review.", "limits": {"timeout_s": 600}}))
    recurrence_file.write_text(json.dumps({"cron": "30 6 * * *", "timezone": "UTC", "max_runs": 5}))
    result = run_cli(create_args("--body-file", str(task_file), "--recurrence-file", str(recurrence_file)))
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_schedule")[0]) == {
        "task": {"agent_id": REVIEWER, "input": "Review.", "limits": {"timeout_s": 600}},
        "recurrence": {"cron": "30 6 * * *", "timezone": "UTC", "max_runs": 5},
    }


def test_create_flags_override_the_same_fields_of_the_recurrence_file(run_cli, fake_farm, tmp_path):
    recurrence_file = tmp_path / "recurrence.json"
    recurrence_file.write_text(json.dumps({"cron": "30 6 * * *", "timezone": "UTC", "max_runs": 5}))
    run_cli(
        create_args(
            REVIEWER,
            "--input",
            "x",
            "--recurrence-file",
            str(recurrence_file),
            "--cron",
            "0 9 * * *",
            "--timezone",
            MADRID,
        )
    )
    assert body_of(sent(fake_farm, "create_schedule")[0])["recurrence"] == {
        "cron": "0 9 * * *",
        "timezone": MADRID,
        "max_runs": 5,
    }


def test_create_sends_the_bounds_of_the_recurrence_in_the_shape_the_api_defines(run_cli, fake_farm):
    result = run_cli(
        create_args(
            REVIEWER,
            "--input",
            "x",
            "--cron",
            WEEKDAYS,
            "--timezone",
            MADRID,
            "--starts-at",
            "2026-11-01T00:00:00Z",
            "--ends-at",
            "2026-12-01T00:00:00Z",
            "--max-runs",
            "10",
            "--overlap",
            "allow",
        )
    )
    assert result.exit_code == 0, result.output
    body = body_of(sent(fake_farm, "create_schedule")[0])
    assert body["recurrence"] == {
        "cron": WEEKDAYS,
        "timezone": MADRID,
        "starts_at": "2026-11-01T00:00:00Z",
        "ends_at": "2026-12-01T00:00:00Z",
        "max_runs": 10,
    }
    assert body["overlap"] == "allow"
    stored = next(iter(fake_farm._schedules.values()))
    assert stored["overlap"] == "allow" and stored["recurrence"]["max_runs"] == 10


def test_create_reads_the_recurrence_from_standard_input(run_cli, fake_farm):
    result = run_cli(
        create_args(REVIEWER, "--input", "x", "--recurrence-file", "-"),
        input=json.dumps({"cron": WEEKDAYS, "timezone": MADRID}),
    )
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_schedule")[0])["recurrence"] == {"cron": WEEKDAYS, "timezone": MADRID}


@pytest.mark.parametrize(
    "args, expected",
    [
        ([REVIEWER, "--input", "x", "--timezone", MADRID], "--cron"),
        ([REVIEWER, "--input", "x", "--cron", WEEKDAYS], "--timezone"),
        (["--cron", WEEKDAYS, "--timezone", MADRID], "AGENT"),
        ([REVIEWER, "--prompt", "x", "--cron", WEEKDAYS, "--timezone", MADRID], "not both"),
        ([REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID, "--overlap", "queue"], "--overlap"),
        ([REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID, "--max-runs", "0"], "--max-runs"),
        (
            [REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID, "--correlation-id", "k"],
            "No such option",
        ),
    ],
)
def test_create_refuses_what_cannot_be_a_schedule_before_any_request(run_cli, fake_farm, args, expected):
    result = run_cli(create_args(*args))
    assert result.exit_code == 2 and expected in result.stderr, result.stderr
    assert fake_farm.requests == []


def test_create_does_not_choose_a_timezone_for_the_person(run_cli, fake_farm):
    result = run_cli(create_args(REVIEWER, "--input", "x", "--cron", WEEKDAYS))
    assert result.exit_code == 2 and "IANA" in result.stderr and fake_farm.requests == []


def test_create_lets_only_one_input_read_standard_input(run_cli, fake_farm):
    result = run_cli(create_args("--body-file", "-", "--recurrence-file", "-"), input="{}")
    assert result.exit_code == 2 and "only one of --body-file, --recurrence-file" in result.stderr
    assert fake_farm.requests == []


def test_create_refuses_a_recurrence_file_that_is_not_a_json_object_without_quoting_it(run_cli, fake_farm, tmp_path):
    bad = tmp_path / "recurrence.json"
    bad.write_text('["0 8 * * *", "secret-looking-text"]')
    result = run_cli(create_args(REVIEWER, "--input", "x", "--recurrence-file", str(bad)))
    assert result.exit_code == 2 and "must hold a JSON object" in result.stderr
    assert "secret-looking-text" not in result.output and fake_farm.requests == []


def test_create_warns_that_the_farm_keeps_the_prompt_and_the_input(run_cli):
    result = run_cli(create_args("--prompt", "hi", "--cron", WEEKDAYS, "--timezone", MADRID))
    assert "the farm stores the prompt and the input" in result.stderr


# -- create: what the farm refuses --------------------------------------------------------------------------------


def test_create_shows_cron_invalid_with_its_code_and_creates_nothing(run_cli, fake_farm):
    result = run_cli(create_args(REVIEWER, "--input", "x", "--cron", "every day", "--timezone", MADRID))
    assert result.exit_code == 1 and "cron_invalid" in result.stderr
    assert fake_farm._schedules == {}


def test_create_shows_timezone_invalid_with_its_code_and_creates_nothing(run_cli, fake_farm):
    result = run_cli(create_args(REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", "Mars/Olympus"))
    assert result.exit_code == 1 and "timezone_invalid" in result.stderr
    assert fake_farm._schedules == {}


def test_create_shows_the_farms_refusal_of_a_task_that_could_never_run(run_cli, fake_farm):
    result = run_cli(create_args("nonexistent-agent", "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID))
    assert result.exit_code == 1 and "agent_not_found" in result.stderr
    assert fake_farm._schedules == {}


def test_create_over_the_tenants_schedule_quota_shows_quota_exceeded(run_cli, fake_farm):
    fake_farm.limits["max_schedules"] = 0
    result = run_cli(create_args(REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID))
    assert result.exit_code == 1 and "quota_exceeded" in result.stderr


def test_create_without_the_write_scope_is_a_403_not_a_crash(run_cli):
    result = run_cli(create_args(REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID), token=READONLY)
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


def test_create_never_repeats_a_request_the_farm_failed_on(run_cli, fake_farm, sleeps):
    fake_farm.fail_next("create_schedule", status=503, code="database_busy", retryable=True)
    result = run_cli(create_args(REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID))
    assert result.exit_code == 1 and "database_busy" in result.stderr
    assert len(sent(fake_farm, "create_schedule")) == 1 and sleeps == []
    assert fake_farm._schedules == {}


# -- list and view ------------------------------------------------------------------------------------------------


def test_list_shows_your_schedules_and_not_other_peoples_or_other_tenants(run_cli, fake_farm):
    mine = make(fake_farm, ALICE)
    make(fake_farm, BOB)
    make(fake_farm, OTHER)
    assert [row[0] for row in rows(run_cli(["schedule", "list"]))] == [mine]


def test_list_prints_id_state_cron_timezone_next_fire_runs_task_and_owner_per_row(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    [row] = rows(run_cli(["schedule", "list"]))
    assert row[:4] == [schedule_id, "active", WEEKDAYS, MADRID]
    assert row[4] != "" and row[5:] == ["0", REVIEWER, "alice"]


def test_list_for_a_tenant_admin_sees_the_whole_tenant_and_owner_me_narrows_it(run_cli, fake_farm):
    make(fake_farm, ALICE)
    make(fake_farm, BOB)
    admins = make(fake_farm, ADMIN)
    assert len(rows(run_cli(["schedule", "list"], token=ADMIN))) == 3
    narrowed = run_cli(["schedule", "list", "--owner", "me"], token=ADMIN)
    assert [row[0] for row in rows(narrowed)] == [admins]
    assert sent(fake_farm, "list_schedules")[-1].query == {"owner": ["me"]}


def test_list_sends_no_query_when_no_filter_is_given(run_cli, fake_farm):
    run_cli(["schedule", "list"])
    assert sent(fake_farm, "list_schedules")[0].query == {}


def test_list_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    make(fake_farm, ALICE)
    assert printed(run_cli(["schedule", "list", "--json"])) == printed(run_cli(["api", "schedules"]))


def test_list_on_a_terminal_draws_headers_and_says_when_there_is_nothing(run_cli, fake_farm, monkeypatch):
    monkeypatch.setattr(output, "_is_tty", lambda stream: True)
    monkeypatch.setenv("NO_COLOR", "1")
    assert run_cli(["schedule", "list"]).stderr == "no results\n"
    make(fake_farm, ALICE)
    header = run_cli(["schedule", "list"]).stdout.splitlines()[0].split()
    assert header == ["ID", "STATE", "CRON", "TIMEZONE", "NEXT", "FIRE", "RUNS", "TASK", "OWNER"]


def test_view_shows_when_it_fires_how_the_last_firing_went_and_what_it_launches(run_cli, fake_farm):
    schedule_id = make(
        fake_farm,
        ALICE,
        task={"agent_id": REVIEWER, "input": "Review my open MRs.\nBe kind.", "scope": "reviews"},
        recurrence={"cron": WEEKDAYS, "timezone": MADRID, "max_runs": 3},
        overlap="allow",
    )
    fake_farm.set_schedule(
        schedule_id,
        last_fire_state="skipped_quota",
        last_fire_error="tenant capacity was full",
        last_fired_at="2026-10-19T06:00:00Z",
        runs_count=2,
        missed_runs=1,
    )
    out = run_cli(["schedule", "view", schedule_id]).stdout
    for expected in (
        schedule_id,
        "state:",
        "active",
        f"{WEEKDAYS} ({MADRID})",
        "max runs:",
        "3",
        "overlap:",
        "allow",
        "next fire:",
        "skipped_quota at 2026-10-19T06:00:00Z: tenant capacity was full",
        "missed runs:",
        "owner:",
        "alice",
        "agent:",
        REVIEWER,
        "scope:",
        "reviews",
    ):
        assert expected in out, expected
    assert out.splitlines()[-2:] == ["  Review my open MRs.", "  Be kind."]


def test_view_cleans_control_characters_out_of_the_stored_prompt(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE, task={"prompt": "hello \x1b[31mred\x1b[0m\x07 world"})
    out = run_cli(["schedule", "view", schedule_id]).stdout
    assert "\x1b" not in out and "\x07" not in out and "hello  [31mred" in out
    assert "(free prompt)" in out


def test_view_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    assert printed(run_cli(["schedule", "view", schedule_id, "--json"])) == printed(
        run_cli(["api", f"schedules/{schedule_id}"])
    )


def test_an_id_with_a_slash_cannot_reach_another_route(run_cli, fake_farm):
    result = run_cli(["schedule", "view", "../whoami"])
    assert result.exit_code == 1
    assert fake_farm.requests[0].path == f"{API}/..%2Fwhoami"
    fake_farm.take_violations()  # the fake has no route for it, which is the point


# -- another person's schedule is a 404 ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["view", "runs", "fire", "pause", "resume", "delete"])
def test_every_command_answers_404_for_another_persons_or_tenants_schedule(run_cli, fake_farm, command):
    bobs = make(fake_farm, BOB)
    theirs = make(fake_farm, OTHER)
    for schedule_id in (bobs, theirs, "no-such-schedule"):
        args = ["schedule", command, schedule_id, *(["--yes"] if command == "delete" else [])]
        result = run_cli(args)
        assert result.exit_code == 1 and "not_found" in result.stderr, (command, schedule_id)
    assert set(fake_farm._schedules) == {bobs, theirs}
    assert fake_farm._tasks == {} and all(s["state"] == "active" for s in fake_farm._schedules.values())


def test_a_tenant_admin_can_operate_a_colleagues_schedule(run_cli, fake_farm):
    alices = make(fake_farm, ALICE)
    assert printed(run_cli(["schedule", "pause", alices, "--json"], token=ADMIN))["state"] == "paused"


# -- runs ---------------------------------------------------------------------------------------------------------


def test_runs_lists_the_tasks_the_schedule_fired_newest_first_with_their_trigger(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE, overlap="allow")
    first = printed(run_cli(["schedule", "fire", schedule_id, "--json"]))["task_id"]
    fake_farm.tick()
    second = printed(run_cli(["schedule", "fire", schedule_id, "--json"]))["task_id"]
    listed = rows(run_cli(["schedule", "runs", schedule_id]))
    assert [(row[0], row[2], row[3]) for row in listed] == [(second, "manual", REVIEWER), (first, "manual", REVIEWER)]


def test_runs_limit_keeps_the_newest_tasks_on_this_side_because_the_farm_never_paginates(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE, overlap="allow")
    for _ in range(3):
        run_cli(["schedule", "fire", schedule_id])
        fake_farm.tick()
    newest = printed(run_cli(["schedule", "runs", schedule_id, "--json"]))["items"][0]["task_id"]
    limited = run_cli(["schedule", "runs", schedule_id, "--limit", "1", "--json"])
    assert [t["task_id"] for t in printed(limited)["items"]] == [newest]
    assert sent(fake_farm, "list_schedule_tasks")[-1].query == {}


def test_runs_of_a_schedule_that_never_fired_is_empty_and_says_so_on_a_terminal(run_cli, fake_farm, monkeypatch):
    schedule_id = make(fake_farm, ALICE)
    assert rows(run_cli(["schedule", "runs", schedule_id])) == []
    monkeypatch.setattr(output, "_is_tty", lambda stream: True)
    assert run_cli(["schedule", "runs", schedule_id]).stderr == "no results\n"


def test_runs_does_not_list_tasks_of_other_schedules(run_cli, fake_farm):
    mine, other = make(fake_farm, ALICE), make(fake_farm, ALICE)
    fired = printed(run_cli(["schedule", "fire", other, "--json"]))["task_id"]
    run_cli(["schedule", "fire", mine])
    assert fired not in run_cli(["schedule", "runs", mine]).stdout


def test_runs_has_no_all_flag_because_there_is_nothing_to_paginate(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    assert run_cli(["schedule", "runs", schedule_id, "--all"]).exit_code == 2


# -- fire ---------------------------------------------------------------------------------------------------------


def test_fire_creates_one_task_now_and_prints_it_with_a_hint_to_follow_it(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    result = run_cli(["schedule", "fire", schedule_id])
    [row] = rows(result)
    task_id = row[0]
    assert row[1:4] == ["queued", "manual", REVIEWER]
    assert f"dynfarm task watch {task_id}" in result.stderr
    assert fake_farm._tasks[task_id].schedule_id == schedule_id
    assert [(r.method, r.status) for r in sent(fake_farm, "fire_schedule")] == [("POST", 201)]


def test_fire_json_is_the_task_the_farm_created(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    created = printed(run_cli(["schedule", "fire", schedule_id, "--json"]))
    assert created["origin"] == "schedule" and created["trigger"] == "manual"
    assert created["correlation_id"].startswith("sched-manual-") and created["state"] == "queued"


def test_fire_does_not_move_the_schedules_counters(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    before = printed(run_cli(["schedule", "view", schedule_id, "--json"]))
    run_cli(["schedule", "fire", schedule_id])
    after = printed(run_cli(["schedule", "view", schedule_id, "--json"]))
    assert (after["next_fire_at"], after["runs_count"], after["state"]) == (
        before["next_fire_at"],
        before["runs_count"],
        before["state"],
    )
    assert after["last_fire_state"] == "ok"


def test_fire_while_the_previous_task_runs_shows_schedule_overlap_and_creates_no_second_task(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    assert run_cli(["schedule", "fire", schedule_id]).exit_code == 0
    again = run_cli(["schedule", "fire", schedule_id])
    assert again.exit_code == 1 and "schedule_overlap" in again.stderr
    assert len(fake_farm._tasks) == 1


def test_fire_with_overlap_allow_launches_in_parallel(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE, overlap="allow")
    assert run_cli(["schedule", "fire", schedule_id]).exit_code == 0
    assert run_cli(["schedule", "fire", schedule_id]).exit_code == 0
    assert len(fake_farm._tasks) == 2


def test_fire_once_the_previous_task_has_finished_is_allowed_again(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    first = printed(run_cli(["schedule", "fire", schedule_id, "--json"]))["task_id"]
    fake_farm.set_task(first, state="succeeded")
    assert run_cli(["schedule", "fire", schedule_id]).exit_code == 0


@pytest.mark.parametrize("state", ["paused", "finished"])
def test_fire_a_schedule_that_is_not_active_shows_schedule_not_active(run_cli, fake_farm, state):
    schedule_id = make(fake_farm, ALICE)
    fake_farm.set_schedule(schedule_id, state=state)
    result = run_cli(["schedule", "fire", schedule_id])
    assert result.exit_code == 1 and "schedule_not_active" in result.stderr and state in result.stderr
    assert fake_farm._tasks == {}


def test_fire_never_repeats_a_request_the_farm_failed_on(run_cli, fake_farm, sleeps):
    schedule_id = make(fake_farm, ALICE)
    fake_farm.fail_next("fire_schedule", status=503, code="cluster_unavailable", retryable=True)
    result = run_cli(["schedule", "fire", schedule_id])
    assert result.exit_code == 1 and "cluster_unavailable" in result.stderr
    assert len(sent(fake_farm, "fire_schedule")) == 1 and sleeps == [] and fake_farm._tasks == {}


def test_fire_without_the_write_scope_is_a_403(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    result = run_cli(["schedule", "fire", schedule_id], token=READONLY)
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr
    assert fake_farm._tasks == {}


# -- pause and resume ---------------------------------------------------------------------------------------------


def test_pause_then_resume_move_the_schedule_between_paused_and_active(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    paused = run_cli(["schedule", "pause", schedule_id])
    assert rows(paused)[0][:2] == [schedule_id, "paused"]
    assert fake_farm._schedules[schedule_id]["state"] == "paused"
    resumed = run_cli(["schedule", "resume", schedule_id])
    assert rows(resumed)[0][:2] == [schedule_id, "active"]
    assert [(r.method, r.status) for r in fake_farm.requests] == [("POST", 200), ("POST", 200)]


def test_pause_and_resume_json_are_the_farms_answer(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    assert printed(run_cli(["schedule", "pause", schedule_id, "--json"]))["state"] == "paused"
    assert printed(run_cli(["schedule", "resume", schedule_id, "--json"]))["state"] == "active"


@pytest.mark.parametrize("command", ["pause", "resume"])
def test_pause_and_resume_a_finished_schedule_show_schedule_finished(run_cli, fake_farm, command):
    schedule_id = make(fake_farm, ALICE)
    fake_farm.set_schedule(schedule_id, state="finished")
    result = run_cli(["schedule", command, schedule_id])
    assert result.exit_code == 1 and "schedule_finished" in result.stderr
    assert fake_farm._schedules[schedule_id]["state"] == "finished"


def test_a_paused_schedule_can_still_be_fired_by_hand_only_after_resuming(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    run_cli(["schedule", "pause", schedule_id])
    assert "schedule_not_active" in run_cli(["schedule", "fire", schedule_id]).stderr
    run_cli(["schedule", "resume", schedule_id])
    assert run_cli(["schedule", "fire", schedule_id]).exit_code == 0


# -- delete -------------------------------------------------------------------------------------------------------


def test_delete_with_yes_deletes_the_schedule_and_says_so(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    result = run_cli(["schedule", "delete", schedule_id, "--yes"])
    assert result.exit_code == 0 and result.stdout == f"deleted schedule {schedule_id}\n"
    assert fake_farm._schedules == {}
    assert [(r.method, r.status) for r in sent(fake_farm, "delete_schedule")] == [("DELETE", 204)]


def test_delete_without_a_terminal_and_without_yes_refuses_and_changes_nothing(run_cli, fake_farm, terminal):
    terminal(False)
    schedule_id = make(fake_farm, ALICE)
    result = run_cli(["schedule", "delete", schedule_id])
    assert result.exit_code == 2 and "--yes" in result.stderr
    assert fake_farm.requests == [] and schedule_id in fake_farm._schedules


def test_delete_asks_on_a_terminal_and_a_no_changes_nothing(run_cli, fake_farm, terminal):
    terminal(True)
    schedule_id = make(fake_farm, ALICE)
    declined = run_cli(["schedule", "delete", schedule_id], input="n\n")
    assert declined.exit_code == 1 and "cancelled" in declined.stderr
    assert fake_farm.requests == [] and schedule_id in fake_farm._schedules
    accepted = run_cli(["schedule", "delete", schedule_id], input="y\n")
    assert accepted.exit_code == 0 and fake_farm._schedules == {}


def test_delete_leaves_the_tasks_it_already_fired(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE)
    task_id = printed(run_cli(["schedule", "fire", schedule_id, "--json"]))["task_id"]
    run_cli(["schedule", "delete", schedule_id, "--yes"])
    assert printed(run_cli(["task", "view", task_id, "--json"]))["task_id"] == task_id


# -- authentication, secrets and wiring ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["schedule", "list"],
        ["schedule", "view", "x"],
        ["schedule", "create", REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID],
        ["schedule", "runs", "x"],
        ["schedule", "fire", "x"],
        ["schedule", "pause", "x"],
        ["schedule", "resume", "x"],
        ["schedule", "delete", "x", "--yes"],
    ],
)
def test_every_schedule_command_without_a_token_exits_4_before_any_request(run_cli, fake_farm, args):
    result = run_cli(args, token=None)
    assert result.exit_code == 4 and fake_farm.requests == []


def test_no_schedule_command_ever_prints_the_token(run_cli, fake_farm):
    schedule_id = make(fake_farm, ALICE, overlap="allow")
    for args in (
        ["schedule", "list"],
        ["--debug", "schedule", "view", schedule_id],
        ["--debug", "schedule", "create", REVIEWER, "--input", "x", "--cron", WEEKDAYS, "--timezone", MADRID],
        ["--debug", "schedule", "runs", schedule_id],
        ["--debug", "schedule", "fire", schedule_id],
        ["--debug", "schedule", "pause", schedule_id],
        ["--debug", "schedule", "resume", schedule_id],
        ["--debug", "schedule", "delete", schedule_id, "--yes"],
    ):
        assert ALICE not in run_cli(args).output, args


def test_the_schedule_commands_declare_the_operations_they_call():
    registry.load_all_commands()
    declared = registry.registered()
    for operation_id, command in (
        ("list_schedules", "schedule.list_"),
        ("get_schedule", "schedule.view"),
        ("create_schedule", "schedule.create"),
        ("list_schedule_tasks", "schedule.runs"),
        ("fire_schedule", "schedule.fire"),
        ("pause_schedule", "schedule.pause"),
        ("resume_schedule", "schedule.resume"),
        ("delete_schedule", "schedule.delete"),
    ):
        assert any(name.endswith(command) for name in declared[operation_id]), (operation_id, command)


def test_the_root_help_and_schedule_help_list_the_commands(run_cli):
    assert "schedule" in run_cli(["--help"]).stdout
    listed = run_cli(["schedule", "--help"]).stdout
    for name in ("list", "view", "create", "runs", "fire", "pause", "resume", "delete"):
        assert f"  {name} " in listed


def test_create_offers_the_flags_of_task_run_except_the_correlation_id(run_cli):
    task_help = run_cli(["task", "run", "--help"]).stdout
    schedule_help = run_cli(["schedule", "create", "--help"]).stdout
    for flag in ("--prompt", "--prompt-file", "--input", "--input-file", "--scope", "--session-ref", "--body-file"):
        assert flag in task_help and flag in schedule_help, flag
    assert "--correlation-id" in task_help and "--correlation-id" not in schedule_help
