"""`dynfarm task`: launch a task, follow it to the end and read what it produced, as a person or a pipeline does.

Why these tests exist: a task is the one thing the farm is for, and every way a command can get it wrong costs money or
trust. `run` must send exactly the body the flags and the body file describe (and nothing at all when they contradict
each other), must not repeat a launch that may have gone through, and must say the farm keeps the prompt. `watch` must
follow the farm's own wait and stop on every terminal state, and Ctrl-C must stop *watching* without cancelling.
`result --download` leaves the farm for the object store and must do it without the farm's token. `cancel` is
destructive, so it asks. Every command must answer 404 for somebody else's task as the farm does, resolve `last` to
the caller's own newest task, and print the farm's answer verbatim under `--json`. The fake farm validates every
request against the pinned OpenAPI, so a body or query the API would reject fails here even when a test does not look.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from fakefarm import ADMIN, ALICE, BOB, OTHER, READONLY

import dynfarm.commands
from dynfarm import registry
from dynfarm.cli import cli
from dynfarm.taskbody import TaskFlags, build_task_body, task_body_options

REVIEWER = "developer-skills:code-reviewer"
TERMINAL = ("succeeded", "failed", "cancelled", "expired")


def body_of(request) -> dict:
    return json.loads(request.body)


def sent(fake_farm, operation_id):
    return [r for r in fake_farm.requests if r.operation_id == operation_id]


def printed(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def id_of(result) -> str:
    """The id `task run` printed (first column of its row, tab-separated because stdout is a pipe)."""
    assert result.exit_code == 0, result.output
    return result.stdout.split("\t")[0]


def amend(fake_farm, operation_id, change):
    """Let `operation_id` answer as the model does, then call `change(body)` to alter the answer before it is sent."""
    holder = {}

    def handler(call):
        status, body = holder["original"](call)
        change(body)
        return status, body

    holder["original"] = fake_farm.override(operation_id, handler)


def walk(fake_farm, task_id, *steps):
    """Make the next `get_task` calls find the task in these states, one per call: what watching a real task is like."""
    queue = list(steps)
    holder = {}

    def step(call):
        if queue:
            fake_farm.set_task(task_id, **queue.pop(0))
        return holder["original"](call)

    holder["original"] = fake_farm.override("get_task", step)


@pytest.fixture
def terminal(monkeypatch):
    """`terminal(True)`: a person is at a terminal (so `cancel` may ask), `terminal(False)`: a script."""

    def choose(tty):
        monkeypatch.setattr(dynfarm.commands, "is_interactive", lambda: tty)

    return choose


# -- run: the body ------------------------------------------------------------------------------------------------


def test_run_with_an_agent_sends_the_agent_and_the_input_and_prints_the_id_and_state(run_cli, fake_farm):
    result = run_cli(["task", "run", REVIEWER, "--input", "Review MR 42."])
    task_id = id_of(result)
    assert result.stdout.split("\t")[1] == "queued"
    assert body_of(sent(fake_farm, "create_task")[0]) == {"agent_id": REVIEWER, "input": "Review MR 42."}
    assert fake_farm._tasks[task_id].data["owner"] == "alice"


def test_run_with_a_prompt_sends_only_the_prompt(run_cli, fake_farm):
    result = run_cli(["task", "run", "--prompt", "Summarise the TODOs."])
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_task")[0]) == {"prompt": "Summarise the TODOs."}


def test_run_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    answer = printed(run_cli(["task", "run", "--prompt", "hi", "--json"]))
    assert set(answer) >= {"task_id", "tenant", "state", "correlation_id", "created_at"}
    assert answer["task_id"] in fake_farm._tasks and answer["tenant"] == "dynapps-internal"


def test_run_tells_on_stderr_that_the_farm_stores_the_prompt_and_the_input(run_cli):
    result = run_cli(["task", "run", "--prompt", "hi"])
    assert "stores the prompt and the input" in result.stderr
    assert "stores" not in result.stdout
    assert result.stderr.count("stores the prompt") == 1


def test_run_does_not_warn_about_stored_text_when_it_sends_none(run_cli):
    result = run_cli(["task", "run", REVIEWER])
    assert result.exit_code == 0 and "stores the prompt" not in result.stderr


def test_run_reads_the_prompt_and_the_input_from_files_without_their_final_newline(run_cli, fake_farm, tmp_path):
    (tmp_path / "prompt.txt").write_text("Line one\nline two\n")
    (tmp_path / "input.txt").write_text("the context\r\n")
    result = run_cli(
        ["task", "run", "--prompt-file", str(tmp_path / "prompt.txt"), "--input-file", str(tmp_path / "input.txt")]
    )
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_task")[0]) == {"prompt": "Line one\nline two", "input": "the context"}


def test_run_reads_the_prompt_from_standard_input(run_cli, fake_farm):
    result = run_cli(["task", "run", "--prompt-file", "-"], input="from a pipe\n")
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_task")[0]) == {"prompt": "from a pipe"}


def test_run_sends_scope_correlation_id_and_session_ref(run_cli, fake_farm):
    result = run_cli(
        ["task", "run", "--prompt", "go", "--scope", "acme-prod", "--correlation-id", "mr-42-1"]
        + ["--session-ref", "sess_01"]
    )
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_task")[0]) == {
        "prompt": "go",
        "scope": "acme-prod",
        "correlation_id": "mr-42-1",
        "session_ref": "sess_01",
    }


def test_run_takes_what_has_no_flag_from_a_body_file(run_cli, fake_farm, tmp_path):
    spec = {
        "agent_id": REVIEWER,
        "input": "Review MR 7.",
        "workspace": {"repo": "https://gitlab.example.test/group/project.git", "ref": "main"},
        "limits": {"timeout_s": 600},
        "callback": {"url": "https://ci.example.test/hook"},
    }
    (tmp_path / "task.json").write_text(json.dumps(spec))
    result = run_cli(["task", "run", "--body-file", str(tmp_path / "task.json")])
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_task")[0]) == spec


def test_run_flags_override_the_same_fields_of_the_body_file(run_cli, fake_farm, tmp_path):
    spec = {"agent_id": "repo-helper", "input": "from the file", "scope": "old", "limits": {"timeout_s": 600}}
    (tmp_path / "task.json").write_text(json.dumps(spec))
    result = run_cli(
        [
            "task",
            "run",
            REVIEWER,
            "--input",
            "from the flag",
            "--scope",
            "new",
            "--body-file",
            str(tmp_path / "task.json"),
        ]
    )
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_task")[0]) == {
        "agent_id": REVIEWER,
        "input": "from the flag",
        "scope": "new",
        "limits": {"timeout_s": 600},
    }


def test_run_reads_the_whole_task_from_standard_input_with_a_dash(run_cli, fake_farm):
    result = run_cli(["task", "run", "--body-file", "-"], input=json.dumps({"prompt": "piped task"}))
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_task")[0]) == {"prompt": "piped task"}


def test_run_with_a_used_correlation_id_says_no_second_task_was_created(run_cli, fake_farm):
    first = id_of(run_cli(["task", "run", "--prompt", "go", "--correlation-id", "same-1"]))
    again = run_cli(["task", "run", "--prompt", "go", "--correlation-id", "same-1"])
    assert id_of(again) == first
    assert "no new task was created" in again.stderr
    assert len(fake_farm._tasks) == 1


# -- run: what is refused before any request ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["task", "run"], "say what to run"),
        (["task", "run", REVIEWER, "--prompt", "both"], "not both"),
        (["task", "run", "--prompt", "x", "--prompt-file", "p.txt"], "--prompt or --prompt-file"),
        (["task", "run", REVIEWER, "--input", "x", "--input-file", "i.txt"], "--input or --input-file"),
        (["task", "run", "--prompt", "   "], "prompt is empty"),
        (["task", "run", "--prompt-file", "-", "--input-file", "-"], "only one of --prompt-file, --input-file"),
        (["task", "run", "--prompt-file", "/no/such/file"], "cannot read --prompt-file /no/such/file"),
        (["task", "run", "--body-file", "/no/such/file"], "cannot read --body-file"),
    ],
)
def test_run_refuses_a_contradiction_with_exit_2_before_any_request(run_cli, fake_farm, args, message):
    result = run_cli(args, input="")
    assert result.exit_code == 2, result.output
    assert message in result.stderr
    assert fake_farm.requests == []


def test_run_refuses_a_body_file_that_is_not_a_json_object_without_quoting_it(run_cli, fake_farm, tmp_path):
    (tmp_path / "bad.json").write_text('{"prompt": "SECRET-LOOKING-TEXT" oops}')
    (tmp_path / "list.json").write_text('["prompt"]')
    bad = run_cli(["task", "run", "--body-file", str(tmp_path / "bad.json")])
    not_object = run_cli(["task", "run", "--body-file", str(tmp_path / "list.json")])
    assert bad.exit_code == 2 and "not valid JSON" in bad.stderr and "SECRET-LOOKING-TEXT" not in bad.stderr
    assert not_object.exit_code == 2 and "must hold a JSON object" in not_object.stderr
    assert fake_farm.requests == []


def test_run_refuses_a_prompt_flag_on_top_of_a_body_file_that_names_an_agent(run_cli, fake_farm, tmp_path):
    (tmp_path / "task.json").write_text(json.dumps({"agent_id": REVIEWER, "input": "x"}))
    result = run_cli(["task", "run", "--prompt", "also", "--body-file", str(tmp_path / "task.json")])
    assert result.exit_code == 2 and "not both" in result.stderr
    assert fake_farm.requests == []


# -- run: what the farm answers -----------------------------------------------------------------------------------


def test_run_shows_the_farms_refusal_of_a_free_prompt_with_its_code(run_cli, fake_farm):
    fake_farm.free_prompt = False
    result = run_cli(["task", "run", "--prompt", "hi"])
    assert result.exit_code == 1
    assert "prompt_not_enabled_for_tenant" in result.stderr


def test_run_with_an_unknown_agent_exits_1_with_agent_not_found(run_cli):
    result = run_cli(["task", "run", "no-such-agent", "--input", "x"])
    assert result.exit_code == 1 and "agent_not_found" in result.stderr


def test_run_without_the_write_scope_is_a_403_not_a_crash(run_cli):
    result = run_cli(["task", "run", "--prompt", "hi"], token=READONLY)
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


def test_run_never_repeats_a_launch_the_farm_failed_on(run_cli, fake_farm, sleeps):
    fake_farm.fail_next("create_task", status=503, code="database_busy", retryable=True)
    result = run_cli(["task", "run", "--prompt", "hi"])
    assert result.exit_code == 1 and "database_busy" in result.stderr
    assert len(sent(fake_farm, "create_task")) == 1 and sleeps == []
    assert fake_farm._tasks == {}


def test_run_without_a_token_exits_4_before_any_request(run_cli, fake_farm):
    result = run_cli(["task", "run", "--prompt", "hi"], token=None)
    assert result.exit_code == 4 and fake_farm.requests == []


def test_run_hints_how_to_follow_the_task_it_launched(run_cli):
    result = run_cli(["task", "run", "--prompt", "hi"])
    assert f"dynfarm task watch {id_of(result)}" in result.stderr


# -- run --watch / --exit-status ----------------------------------------------------------------------------------


def test_run_watch_follows_the_task_and_prints_the_finished_task(run_cli, fake_farm):
    amend(
        fake_farm,
        "create_task",
        lambda created: walk(fake_farm, created["task_id"], {"state": "running"}, {"state": "succeeded"}),
    )
    result = run_cli(["task", "run", "--prompt", "hi", "--watch"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].split("\t")[1] == "queued"  # the launch row, then the finished task
    assert "state:" in result.stdout and "succeeded" in result.stdout
    assert [line for line in result.stderr.splitlines() if line in ("running", "succeeded")] == ["running", "succeeded"]


def test_run_exit_status_implies_watch_and_exits_1_when_the_task_fails(run_cli, fake_farm):
    amend(
        fake_farm,
        "create_task",
        lambda created: walk(fake_farm, created["task_id"], {"state": "failed", "failure_reason": "timeout"}),
    )
    result = run_cli(["task", "run", "--prompt", "hi", "--exit-status"])
    assert result.exit_code == 1
    assert "failed (timeout)" in result.stderr and "failed (timeout)" in result.stdout


def test_run_watch_json_prints_one_document_the_finished_task(run_cli, fake_farm):
    amend(fake_farm, "create_task", lambda created: walk(fake_farm, created["task_id"], {"state": "succeeded"}))
    answer = printed(run_cli(["task", "run", "--prompt", "hi", "--watch", "--json"]))
    assert answer["state"] == "succeeded" and answer["prompt"] == "hi"


# -- list ---------------------------------------------------------------------------------------------------------


def test_list_prints_one_row_per_task_newest_first(run_cli, fake_farm):
    older = fake_farm.seed_task(ALICE, state="succeeded")
    fake_farm.tick()
    newer = fake_farm.seed_task(ALICE, state="running", agent_id="repo-helper")
    rows = [line.split("\t") for line in run_cli(["task", "list"]).stdout.splitlines()]
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (newer, "repo-helper", "running"),
        (older, REVIEWER, "succeeded"),
    ]
    assert rows[0][3] == "2026-10-20T12:01:00Z" and rows[0][4] == ""  # created as sent; no owner for a non-admin


def test_list_draws_headers_on_a_terminal(run_cli, fake_farm, monkeypatch):
    import dynfarm.output as output

    fake_farm.seed_task(ALICE)
    monkeypatch.setattr(output, "_is_tty", lambda stream: True)
    monkeypatch.setenv("NO_COLOR", "1")
    assert run_cli(["task", "list"]).stdout.splitlines()[0].split() == ["ID", "AGENT", "STATE", "CREATED", "OWNER"]


def test_list_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    fake_farm.seed_task(ALICE)
    assert printed(run_cli(["task", "list", "--json"])) == printed(run_cli(["api", "tasks"]))


def test_list_sends_each_filter_to_the_farm(run_cli, fake_farm):
    run_cli(
        ["task", "list", "--state", "queued", "--state", "running", "--agent", REVIEWER, "--scope", "acme-prod"]
        + ["--owner", "me", "--since", "2026-10-01T00:00:00Z", "--until", "2026-11-01T00:00:00Z", "--limit", "7"]
    )
    assert sent(fake_farm, "list_tasks")[0].query == {
        "state": ["queued,running"],
        "agent_id": [REVIEWER],
        "scope": ["acme-prod"],
        "owner": ["me"],
        "since": ["2026-10-01T00:00:00Z"],
        "until": ["2026-11-01T00:00:00Z"],
        "limit": ["7"],
    }


def test_list_filters_by_state_and_scope_the_way_the_farm_does(run_cli, fake_farm):
    keep = fake_farm.seed_task(ALICE, state="running", scope="acme-prod")
    fake_farm.seed_task(ALICE, state="succeeded", scope="acme-prod")
    fake_farm.seed_task(ALICE, state="running", scope="other")
    rows = run_cli(["task", "list", "--state", "running", "--scope", "acme-prod"]).stdout.splitlines()
    assert [row.split("\t")[0] for row in rows] == [keep]


def test_list_asks_for_a_single_page_and_says_how_to_get_the_next(run_cli, fake_farm):
    ids = [fake_farm.seed_task(ALICE) for _ in range(3)]
    first = run_cli(["task", "list", "--limit", "2"])
    assert len(first.stdout.splitlines()) == 2
    assert "--cursor 2" in first.stderr
    second = run_cli(["task", "list", "--limit", "2", "--cursor", "2"])
    assert [row.split("\t")[0] for row in second.stdout.splitlines()] == [ids[0]]
    assert "--cursor" not in second.stderr


def test_list_all_follows_next_cursor_to_the_end(run_cli, fake_farm):
    ids = [fake_farm.seed_task(ALICE) for _ in range(5)]
    result = run_cli(["task", "list", "--all", "--limit", "2"])
    assert [row.split("\t")[0] for row in result.stdout.splitlines()] == list(reversed(ids))
    assert len(sent(fake_farm, "list_tasks")) == 3


def test_list_all_json_merges_the_pages_into_one_listing(run_cli, fake_farm):
    for _ in range(3):
        fake_farm.seed_task(ALICE)
    answer = printed(run_cli(["task", "list", "--all", "--limit", "2", "--json"]))
    assert len(answer["items"]) == 3 and answer["next_cursor"] is None


def test_list_all_stops_when_the_farm_keeps_sending_the_same_cursor(run_cli, fake_farm):
    fake_farm.override("list_tasks", lambda call: (200, {"items": [], "next_cursor": "same"}))
    result = run_cli(["task", "list", "--all"])
    assert result.exit_code == 1 and "pagination_loop" in result.stderr
    assert len(sent(fake_farm, "list_tasks")) == 2


def test_list_shows_only_the_callers_tasks_and_a_tenant_admin_sees_the_owners(run_cli, fake_farm):
    mine = fake_farm.seed_task(ALICE)
    bobs = fake_farm.seed_task(BOB)
    theirs = fake_farm.seed_task(OTHER)
    as_alice = [row.split("\t")[0] for row in run_cli(["task", "list"]).stdout.splitlines()]
    as_admin = [row.split("\t") for row in run_cli(["task", "list"], token=ADMIN).stdout.splitlines()]
    assert as_alice == [mine]
    assert {(row[0], row[4]) for row in as_admin} == {(mine, "alice"), (bobs, "bob")}
    assert theirs not in "".join(map("\t".join, as_admin))


def test_list_owner_me_narrows_a_tenant_admin_to_their_own(run_cli, fake_farm):
    fake_farm.seed_task(ALICE)
    own = fake_farm.seed_task(ADMIN)
    rows = run_cli(["task", "list", "--owner", "me"], token=ADMIN).stdout.splitlines()
    assert [row.split("\t")[0] for row in rows] == [own]


def test_list_shows_the_farms_rejection_of_an_unknown_state(run_cli):
    result = run_cli(["task", "list", "--state", "sleeping"])
    assert result.exit_code == 1 and "state_invalid" in result.stderr


def test_list_refuses_a_page_size_the_farm_would_reject_before_asking(run_cli, fake_farm):
    result = run_cli(["task", "list", "--limit", "101"])
    assert result.exit_code == 2 and fake_farm.requests == []


def test_list_with_no_tasks_prints_nothing(run_cli):
    result = run_cli(["task", "list"])
    assert result.exit_code == 0 and result.stdout == ""


# -- view and last ------------------------------------------------------------------------------------------------


def test_view_shows_the_task_state_agent_times_and_the_prompt_as_a_block(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running", agent_id=None, prompt="First line\nSecond line", scope="docs")
    shown = run_cli(["task", "view", task_id]).stdout.splitlines()
    assert f"id:             {task_id}" in shown
    assert any(line.startswith("state:") and line.endswith("running") for line in shown)
    assert any(line.startswith("agent:") and line.endswith("(free prompt)") for line in shown)
    assert any(line.startswith("scope:") and line.endswith("docs") for line in shown)
    assert shown[-3:] == ["prompt:", "  First line", "  Second line"]


def test_view_explains_a_failed_task_and_a_queued_one(run_cli, fake_farm):
    failed = fake_farm.seed_task(ALICE, state="failed")
    fake_farm.set_task(failed, failure_reason="oom")
    queued = fake_farm.seed_task(ALICE, state="queued")
    fake_farm.set_task(queued, queue_position=2)
    assert "failed (oom)" in run_cli(["task", "view", failed]).stdout
    assert "queued (position 2)" in run_cli(["task", "view", queued]).stdout


def test_view_cleans_control_characters_out_of_what_other_text_wrote(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, agent_id=None, prompt="hello \x1b[31mred\x1b[0m\x07 world")
    out = run_cli(["task", "view", task_id]).stdout
    assert "\x1b" not in out and "\x07" not in out and "hello  [31mred" in out


def test_view_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    assert printed(run_cli(["task", "view", task_id, "--json"])) == printed(run_cli(["api", f"tasks/{task_id}"]))


@pytest.mark.parametrize("command", ["view", "logs", "result", "watch", "cancel"])
def test_every_command_answers_404_for_another_persons_or_tenants_task(run_cli, fake_farm, command):
    bobs = fake_farm.seed_task(BOB, state="succeeded")
    theirs = fake_farm.seed_task(OTHER, state="succeeded")
    for task_id in (bobs, theirs, "no-such-task"):
        result = run_cli(["task", command, task_id, "--yes"] if command == "cancel" else ["task", command, task_id])
        assert result.exit_code == 1 and "task_not_found" in result.stderr, (command, task_id)
    assert sent(fake_farm, "cancel_task") == [] or all(r.status == 404 for r in sent(fake_farm, "cancel_task"))
    assert fake_farm._tasks[bobs].data["state"] == "succeeded"


def test_an_id_with_a_slash_cannot_reach_another_route(run_cli, fake_farm):
    result = run_cli(["task", "view", "../whoami"])
    assert result.exit_code == 1
    assert fake_farm.requests[0].path == "/farm/api/v1/tasks/..%2Fwhoami"
    fake_farm.take_violations()  # the fake has no route for it, which is the point


def test_last_is_the_newest_task_of_the_caller_not_of_the_tenant(run_cli, fake_farm):
    mine = fake_farm.seed_task(ALICE)
    fake_farm.tick()
    fake_farm.seed_task(BOB)
    assert printed(run_cli(["task", "view", "last", "--json"]))["task_id"] == mine
    assert sent(fake_farm, "list_tasks")[0].query == {"owner": ["me"], "limit": ["1"]}


def test_last_for_a_tenant_admin_is_their_own_task_not_somebody_elses(run_cli, fake_farm):
    fake_farm.seed_task(ALICE)
    own = fake_farm.seed_task(ADMIN)
    assert printed(run_cli(["task", "view", "last", "--json"], token=ADMIN))["task_id"] == own


def test_last_with_no_tasks_says_so_and_exits_1(run_cli):
    result = run_cli(["task", "view", "last"])
    assert result.exit_code == 1 and "no tasks yet" in result.stderr


# -- watch --------------------------------------------------------------------------------------------------------


def test_watch_prints_each_state_change_on_stderr_and_the_finished_task_on_stdout(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="queued")
    walk(
        fake_farm,
        task_id,
        {},
        {"queue_position": 1},
        {"state": "running", "queue_position": None},
        {"state": "running"},
        {"state": "succeeded"},
    )
    result = run_cli(["task", "watch", task_id])
    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == ["queued", "queued (position 1)", "running", "succeeded"]
    assert any(line.startswith("state:") and line.endswith("succeeded") for line in result.stdout.splitlines())


def test_watch_asks_the_farm_to_hold_each_request_for_30_seconds(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    walk(fake_farm, task_id, {"state": "running"}, {"state": "succeeded"})
    run_cli(["task", "watch", task_id])
    assert [r.query for r in sent(fake_farm, "get_task")] == [{"wait": ["30"]}] * 2


def test_watch_pauses_when_the_farm_answers_at_once_instead_of_spinning(run_cli, fake_farm, sleeps):
    task_id = fake_farm.seed_task(ALICE, state="running")
    walk(fake_farm, task_id, {"state": "running"}, {"state": "running"}, {"state": "succeeded"})
    run_cli(["task", "watch", task_id])
    assert sleeps == [2.0, 2.0]


@pytest.mark.parametrize("state", TERMINAL)
def test_watch_stops_at_every_terminal_state(run_cli, fake_farm, state):
    task_id = fake_farm.seed_task(ALICE, state=state)
    result = run_cli(["task", "watch", task_id])
    assert result.exit_code == 0 and len(sent(fake_farm, "get_task")) == 1
    assert result.stderr.splitlines() == [state]


def test_watch_exit_status_is_0_for_succeeded_and_1_for_anything_else(run_cli, fake_farm):
    ok = fake_farm.seed_task(ALICE, state="succeeded")
    bad = {state: fake_farm.seed_task(ALICE, state=state) for state in ("failed", "cancelled", "expired")}
    assert run_cli(["task", "watch", ok, "--exit-status"]).exit_code == 0
    for task_id in bad.values():
        assert run_cli(["task", "watch", task_id, "--exit-status"]).exit_code == 1


def test_watch_without_exit_status_is_0_even_for_a_failed_task(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="failed")
    fake_farm.set_task(task_id, failure_reason="timeout")
    result = run_cli(["task", "watch", task_id])
    assert result.exit_code == 0 and "failed (timeout)" in result.stdout


def test_watch_json_prints_only_the_finished_task_on_stdout(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    walk(fake_farm, task_id, {"state": "succeeded"})
    result = run_cli(["task", "watch", task_id, "--json"])
    assert json.loads(result.stdout)["state"] == "succeeded"
    assert result.stderr.splitlines() == ["succeeded"]


def test_watch_last_follows_the_callers_newest_task(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    walk(fake_farm, task_id, {"state": "succeeded"})
    assert run_cli(["task", "watch", "last"]).exit_code == 0
    assert sent(fake_farm, "get_task")[0].path.endswith(task_id)


def test_ctrl_c_stops_watching_and_says_the_task_keeps_running(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")

    def interrupted(call):
        raise KeyboardInterrupt

    fake_farm.override("get_task", interrupted)
    result = run_cli(["task", "watch", task_id])
    assert result.exit_code == 130
    assert "the task keeps running" in result.stderr and f"dynfarm task watch {task_id}" in result.stderr
    assert sent(fake_farm, "cancel_task") == []
    assert fake_farm._tasks[task_id].data["state"] == "running"


def test_watch_shows_an_error_the_farm_gives_midway_and_keeps_its_exit_code(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    result = run_cli(["task", "watch", task_id], token="tok-nobody")
    assert result.exit_code == 4 and "rejected" in result.stderr


# -- logs ---------------------------------------------------------------------------------------------------------


def test_logs_prints_one_line_per_log_line_on_stdout(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running", logs=["[run-agent] start", "step 1", "step 2"])
    result = run_cli(["task", "logs", task_id])
    assert result.stdout.splitlines() == ["[run-agent] start", "step 1", "step 2"]
    assert sent(fake_farm, "get_logs")[0].query == {}


def test_logs_tail_asks_for_the_last_lines_only(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running", logs=[f"line {n}" for n in range(10)])
    result = run_cli(["task", "logs", task_id, "--tail", "3"])
    assert result.stdout.splitlines() == ["line 7", "line 8", "line 9"]
    assert sent(fake_farm, "get_logs")[0].query == {"tail_lines": ["3"]}


def test_logs_of_a_finished_task_link_the_complete_log_on_stderr(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded", logs=["done"])
    result = run_cli(["task", "logs", task_id])
    assert result.stdout == "done\n"
    assert f"complete log: {fake_farm.base_url}/_s3/{task_id}/logs" in result.stderr


def test_logs_of_a_task_that_has_not_started_say_so_without_failing(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="queued")
    result = run_cli(["task", "logs", task_id])
    assert result.exit_code == 0 and result.stdout == ""
    assert "no log lines yet (the task is queued)" in result.stderr


def test_logs_neutralise_terminal_escapes_but_keep_tabs(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running", logs=["a\tb \x1b[2J\x1b]0;pwned\x07 end\r"])
    out = run_cli(["task", "logs", task_id]).stdout
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out and "a\tb" in out


def test_logs_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running", logs=["x"])
    assert printed(run_cli(["task", "logs", task_id, "--json"])) == printed(run_cli(["api", f"tasks/{task_id}/logs"]))


def test_logs_refuses_a_tail_the_farm_would_reject(run_cli, fake_farm):
    assert run_cli(["task", "logs", "x", "--tail", "1001"]).exit_code == 2
    assert fake_farm.requests == []


# -- result -------------------------------------------------------------------------------------------------------


def test_result_shows_the_summary_the_usage_and_the_link(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    out = run_cli(["task", "result", task_id]).stdout
    assert out.startswith("summary:\n  The change looks fine.\n")
    assert "cost:" in out and "0.01 EUR" in out and "duration:" in out and "5 s" in out
    assert f"{fake_farm.base_url}/_s3/{task_id}/result.json" in out
    assert "link valid until:" in out and "size:" in out and " bytes" in out


def test_result_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    assert printed(run_cli(["task", "result", task_id, "--json"])) == printed(
        run_cli(["api", f"tasks/{task_id}/result"])
    )


@pytest.mark.parametrize(
    ("state", "code"),
    [("running", "task_not_terminal"), ("failed", "task_has_no_result"), ("expired", "result_expired")],
)
def test_result_of_a_task_without_a_result_shows_why(run_cli, fake_farm, state, code):
    task_id = fake_farm.seed_task(ALICE, state=state)
    result = run_cli(["task", "result", task_id])
    assert result.exit_code == 1 and code in result.stderr


def test_result_download_saves_the_file_and_never_sends_the_farms_token_to_the_store(run_cli, fake_farm, tmp_path):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    target = tmp_path / "result.json"
    result = run_cli(["task", "result", task_id, "--download", str(target)])
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text())["result"] == "The change looks fine."
    assert f"saved {target.stat().st_size} bytes" in result.stderr
    store = [r for r in fake_farm.requests if r.path.startswith("/_s3/")]
    assert len(store) == 1
    assert "authorization" not in store[0].headers and ALICE not in store[0].text
    assert ALICE not in result.output


def test_result_download_refuses_to_overwrite_without_force_and_asks_the_farm_nothing(run_cli, fake_farm, tmp_path):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    target = tmp_path / "result.json"
    target.write_text("keep me")
    result = run_cli(["task", "result", task_id, "--download", str(target)])
    assert result.exit_code == 2 and "--force" in result.stderr
    assert target.read_text() == "keep me" and fake_farm.requests == []


def test_result_download_replaces_the_file_with_force(run_cli, fake_farm, tmp_path):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    target = tmp_path / "result.json"
    target.write_text("old")
    assert run_cli(["task", "result", task_id, "--download", str(target), "--force"]).exit_code == 0
    assert json.loads(target.read_text())["task_id"] == task_id


def test_result_download_into_a_missing_directory_is_refused_before_asking(run_cli, fake_farm, tmp_path):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    result = run_cli(["task", "result", task_id, "--download", str(tmp_path / "nope" / "r.json")])
    assert result.exit_code == 2 and "does not exist" in result.stderr and fake_farm.requests == []


def test_result_download_keeps_nothing_when_the_checksum_does_not_match(run_cli, fake_farm, tmp_path):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    amend(fake_farm, "get_result", lambda body: body["result"].update(sha256="0" * 64))
    target = tmp_path / "result.json"
    result = run_cli(["task", "result", task_id, "--download", str(target)])
    assert result.exit_code == 1 and "does not match the sha256" in result.stderr
    assert list(tmp_path.iterdir()) == []  # neither the file nor a half-written temporary one


def test_result_download_refuses_a_link_that_is_not_https(run_cli, fake_farm, tmp_path):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    amend(
        fake_farm,
        "get_result",
        lambda body: body["result"].update(url="http://objects.example.test/r.json?X-Signature=abc"),
    )
    result = run_cli(["task", "result", task_id, "--download", str(tmp_path / "r.json")])
    assert result.exit_code == 1 and "not an https address" in result.stderr
    assert [r for r in fake_farm.requests if r.path.startswith("/_s3/")] == []
    assert "X-Signature" not in result.output


def test_result_download_failure_names_the_host_not_the_signed_url(run_cli, fake_farm, tmp_path):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    gone = f"{fake_farm.base_url}/_s3/no-such-task/result.json?X-Signature=SIGNED"
    amend(fake_farm, "get_result", lambda body: body["result"].update(url=gone))
    result = run_cli(["task", "result", task_id, "--download", str(tmp_path / "r.json")])
    assert result.exit_code == 1 and "HTTP 404" in result.stderr
    assert "SIGNED" not in result.output and list(tmp_path.iterdir()) == []


def test_result_download_over_a_real_socket_sends_no_authorization_either(fake_farm, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    target = tmp_path / "result.json"
    with fake_farm.serve() as url:
        from dynfarm import _brand
        from dynfarm.context import AppContext

        monkeypatch.setenv(_brand.env("HOST"), url)
        app = AppContext(token_provider=lambda host: ALICE)
        result = CliRunner().invoke(cli, ["task", "result", task_id, "--download", str(target)], obj=app)
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text())["task_id"] == task_id
    store = [r for r in fake_farm.requests if r.path.startswith("/_s3/")]
    assert len(store) == 1 and "authorization" not in store[0].headers


# -- cancel -------------------------------------------------------------------------------------------------------


def test_cancel_with_yes_cancels_the_task_and_says_so(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    result = run_cli(["task", "cancel", task_id, "--yes"])
    assert result.exit_code == 0 and result.stdout == f"cancelled task {task_id}\n"
    assert fake_farm._tasks[task_id].data["state"] == "cancelled"
    assert [(r.method, r.status) for r in sent(fake_farm, "cancel_task")] == [("DELETE", 202)]


def test_cancel_without_a_terminal_and_without_yes_refuses_and_changes_nothing(run_cli, fake_farm, terminal):
    terminal(False)
    task_id = fake_farm.seed_task(ALICE, state="running")
    result = run_cli(["task", "cancel", task_id])
    assert result.exit_code == 2 and "pass --yes" in result.stderr
    assert sent(fake_farm, "cancel_task") == [] and fake_farm._tasks[task_id].data["state"] == "running"


def test_cancel_asks_on_a_terminal_and_a_no_changes_nothing(run_cli, fake_farm, terminal):
    terminal(True)
    task_id = fake_farm.seed_task(ALICE, state="running")
    declined = run_cli(["task", "cancel", task_id], input="n\n")
    assert declined.exit_code == 1 and "nothing was changed" in declined.stderr
    assert f"Cancel task {task_id}?" in declined.stderr and fake_farm._tasks[task_id].data["state"] == "running"
    accepted = run_cli(["task", "cancel", task_id], input="y\n")
    assert accepted.exit_code == 0 and fake_farm._tasks[task_id].data["state"] == "cancelled"


def test_cancel_of_a_finished_task_shows_the_farms_conflict(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    result = run_cli(["task", "cancel", task_id, "--yes"])
    assert result.exit_code == 1 and "task_not_cancellable" in result.stderr


def test_cancel_last_cancels_the_callers_newest_task(run_cli, fake_farm):
    fake_farm.seed_task(ALICE, state="running")
    fake_farm.tick()
    newest = fake_farm.seed_task(ALICE, state="running")
    fake_farm.tick()
    fake_farm.seed_task(BOB, state="running")
    assert run_cli(["task", "cancel", "last", "--yes"]).stdout == f"cancelled task {newest}\n"


def test_cancel_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    answer = printed(run_cli(["task", "cancel", task_id, "--yes", "--json"]))
    assert answer["task_id"] == task_id and answer["state"] == "cancelled"


def test_cancel_without_the_cancel_scope_is_a_403(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    result = run_cli(["task", "cancel", task_id, "--yes"], token=READONLY)
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


# -- the token, the registry and the help -------------------------------------------------------------------------


def test_no_task_command_ever_prints_the_token(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded", logs=["x"])
    for args in (
        ["task", "run", "--prompt", "hi"],
        ["task", "list"],
        ["task", "view", task_id],
        ["task", "watch", task_id],
        ["task", "logs", task_id],
        ["task", "result", task_id],
        ["--debug", "task", "view", task_id],
        ["task", "cancel", "no-such-task", "--yes"],
    ):
        assert ALICE not in run_cli(args).output, args


def test_a_token_from_another_tenant_sees_nothing_of_these_tasks(run_cli, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    assert run_cli(["task", "list"], token=OTHER).stdout == ""
    assert run_cli(["task", "view", task_id], token=OTHER).exit_code == 1


def test_the_task_commands_declare_the_operations_they_call():
    registry.load_all_commands()
    declared = registry.registered()
    for operation_id, command in (
        ("create_task", "task.run"),
        ("list_tasks", "task.list_"),
        ("get_task", "task.view"),
        ("get_task", "task.watch"),
        ("get_logs", "task.logs"),
        ("get_result", "task.result"),
        ("cancel_task", "task.cancel"),
    ):
        assert any(name.endswith(command) for name in declared[operation_id]), (operation_id, command)


def test_the_root_help_and_task_help_list_the_commands(run_cli):
    assert "task" in run_cli(["--help"]).stdout
    listed = run_cli(["task", "--help"]).stdout
    for name in ("run", "list", "view", "watch", "logs", "result", "cancel"):
        assert f"  {name} " in listed


# -- the shared task flags (the schedule commands reuse them) -----------------------------------------------------


def test_the_task_flags_can_leave_out_the_correlation_id_for_a_schedules_task():
    import click

    @click.command()
    @click.argument("agent", required=False)
    @task_body_options(correlation_id=False)
    def build(agent, task_flags):
        click.echo(json.dumps(build_task_body(task_flags, agent=agent)))

    assert "--correlation-id" not in CliRunner().invoke(build, ["--help"]).output
    result = CliRunner().invoke(build, [REVIEWER, "--input", "x", "--scope", "s"])
    assert json.loads(result.output) == {"agent_id": REVIEWER, "input": "x", "scope": "s"}
    assert CliRunner().invoke(build, [REVIEWER, "--correlation-id", "x"]).exit_code == 2


def test_building_a_body_needs_no_command_at_all():
    assert build_task_body(TaskFlags(prompt="hi", scope="s")) == {"prompt": "hi", "scope": "s"}
