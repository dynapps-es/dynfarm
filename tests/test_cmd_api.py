"""`dynfarm api` as a script meets it: any endpoint, JSON out, and no way to point the token somewhere else.

Why these tests exist: `api` is the escape hatch for everything the curated commands do not cover, so it has to be
trustworthy on its own. Fields go where the method says (query on GET, JSON body otherwise) and typed fields become
real JSON types; a paginated listing is merged completely; the farm's errors keep their exit codes (4 for an
authentication failure, 1 for the rest); and the one thing that must not be possible is sending the token anywhere
but the selected farm's API, so a full URL, a `..` segment and an `Authorization` override are refused before any
request leaves. The fake farm validates every request against the pinned OpenAPI, so a body the API would reject
fails here too.
"""

from __future__ import annotations

import json

import pytest
from fakefarm import ALICE, BOB, EXPIRED, OTHER

from dynfarm import registry


def body_of(request) -> dict:
    return json.loads(request.body)


def printed(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# -- reading ------------------------------------------------------------------------------------------------------


def test_a_plain_call_prints_the_farms_answer_as_json(run_cli, fake_farm):
    answer = printed(run_cli(["api", "whoami"]))
    assert answer["subject"] == "alice" and answer["tenant"] == "dynapps-internal"
    assert [(r.method, r.path) for r in fake_farm.requests] == [("GET", "/farm/api/v1/whoami")]


@pytest.mark.parametrize("path", ["whoami", "/whoami", "/farm/api/v1/whoami", "farm/api/v1/whoami"])
def test_the_path_may_be_relative_or_carry_the_api_prefix(run_cli, fake_farm, path):
    assert printed(run_cli(["api", path]))["subject"] == "alice"
    assert fake_farm.requests[-1].path == "/farm/api/v1/whoami"


def test_fields_become_the_query_string_on_a_get(run_cli, fake_farm):
    fake_farm.seed_task(ALICE, state="running")
    fake_farm.seed_task(ALICE, state="succeeded")
    answer = printed(run_cli(["api", "-X", "GET", "tasks", "-f", "state=running", "-F", "limit=5"]))
    assert [item["state"] for item in answer["items"]] == ["running"]
    assert fake_farm.requests[-1].query == {"state": ["running"], "limit": ["5"]}
    assert fake_farm.requests[-1].method == "GET"


def test_a_query_written_in_the_path_is_sent_and_a_field_overrides_it(run_cli, fake_farm):
    fake_farm.seed_task(ALICE, state="running")
    printed(run_cli(["api", "-X", "GET", "tasks?state=queued&limit=3", "-f", "state=running"]))
    assert fake_farm.requests[-1].query == {"state": ["running"], "limit": ["3"]}


def test_the_token_of_the_selected_person_is_used_so_each_tenant_sees_its_own(run_cli, fake_farm):
    seeded = fake_farm.seed_task(ALICE)
    mine = printed(run_cli(["api", "tasks"], token=ALICE))
    theirs = printed(run_cli(["api", "tasks"], token=OTHER))
    assert [item["task_id"] for item in mine["items"]] == [seeded]
    assert theirs["items"] == []


# -- writing ------------------------------------------------------------------------------------------------------


def test_fields_become_a_json_body_when_the_method_is_not_get(run_cli, fake_farm):
    result = run_cli(["api", "-X", "POST", "tasks", "-f", "agent_id=developer-skills:code-reviewer", "-f", "scope=ci"])
    assert printed(result)["state"] == "queued"
    sent = fake_farm.requests[-1]
    assert sent.method == "POST" and body_of(sent) == {"agent_id": "developer-skills:code-reviewer", "scope": "ci"}


def test_giving_fields_without_a_method_means_post_as_in_gh(run_cli, fake_farm):
    printed(run_cli(["api", "tasks", "-f", "agent_id=developer-skills:code-reviewer"]))
    assert fake_farm.requests[-1].method == "POST"


def test_typed_fields_become_booleans_null_and_integers_while_raw_fields_stay_strings(run_cli, fake_farm):
    fake_farm.violations.clear()
    run_cli(
        ["api", "tasks", "-F", "a=true", "-F", "b=false", "-F", "c=null", "-F", "d=42", "-F", "e=-7", "-f", "f=42"]
        + ["-f", "g=true", "-F", "h=text"]
    )
    assert body_of(fake_farm.requests[-1]) == {
        "a": True,
        "b": False,
        "c": None,
        "d": 42,
        "e": -7,
        "f": "42",
        "g": "true",
        "h": "text",
    }
    fake_farm.take_violations()  # these are not task fields; the point is what was sent


def test_a_typed_field_can_read_its_text_from_a_file_or_from_standard_input(run_cli, fake_farm, tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this\nplease\n")
    printed(run_cli(["api", "tasks", "-F", f"prompt=@{prompt}"]))
    assert body_of(fake_farm.requests[-1]) == {"prompt": "review this\nplease\n"}
    printed(run_cli(["api", "tasks", "-F", "prompt=@-"], input="from stdin"))
    assert body_of(fake_farm.requests[-1]) == {"prompt": "from stdin"}


def test_input_from_standard_input_is_the_json_body(run_cli, fake_farm):
    payload = {"prompt": "hello", "mcp_servers": []}
    answer = printed(run_cli(["api", "tasks", "--input", "-"], input=json.dumps(payload)))
    assert answer["state"] == "queued"
    assert body_of(fake_farm.requests[-1]) == payload


def test_input_from_a_file_with_fields_sends_the_fields_in_the_query(run_cli, fake_farm, tmp_path):
    body = tmp_path / "task.json"
    body.write_text(json.dumps({"agent_id": "developer-skills:code-reviewer"}))
    printed(run_cli(["api", "tasks", "--input", str(body), "-f", "note=x"]))
    sent = fake_farm.requests[-1]
    assert sent.method == "POST" and sent.query == {"note": ["x"]}
    assert body_of(sent) == {"agent_id": "developer-skills:code-reviewer"}
    fake_farm.take_violations()  # `note` is not an API parameter; the point is where it went


def test_a_call_with_an_empty_answer_prints_nothing_and_succeeds(run_cli, fake_farm):
    schedule = {
        "task": {"agent_id": "developer-skills:code-reviewer"},
        "recurrence": {"cron": "0 9 * * 1", "timezone": "Europe/Madrid"},
    }
    created = printed(run_cli(["api", "schedules", "--input", "-"], input=json.dumps(schedule)))
    result = run_cli(["api", "-X", "DELETE", f"schedules/{created['schedule_id']}"])
    assert result.exit_code == 0 and result.stdout == ""
    assert fake_farm.requests[-1].method == "DELETE" and fake_farm.requests[-1].status == 204


def test_a_post_action_without_fields_needs_the_method_to_be_said(run_cli, fake_farm):
    task = fake_farm.seed_task(ALICE, state="running")
    printed(run_cli(["api", "-X", "DELETE", f"tasks/{task}"]))
    assert fake_farm.requests[-1].method == "DELETE"
    assert printed(run_cli(["api", f"tasks/{task}"]))["state"] == "cancelled"


# -- pagination ---------------------------------------------------------------------------------------------------


def test_paginate_follows_the_cursor_across_three_pages_and_merges_the_items(run_cli, fake_farm):
    seeded = {fake_farm.seed_task(ALICE) for _ in range(5)}
    result = run_cli(["api", "tasks", "--paginate", "-F", "limit=2"])
    answer = printed(result)
    assert {item["task_id"] for item in answer["items"]} == seeded and len(answer["items"]) == 5
    assert answer["next_cursor"] is None
    pages = [r for r in fake_farm.requests if r.path.endswith("/tasks")]
    assert len(pages) == 3
    assert [r.query.get("cursor") for r in pages] == [None, ["2"], ["4"]]
    assert all(r.query["limit"] == ["2"] for r in pages)


def test_paginate_on_a_single_page_returns_it_whole(run_cli, fake_farm):
    seeded = fake_farm.seed_task(ALICE)
    answer = printed(run_cli(["api", "tasks", "--paginate"]))
    assert [item["task_id"] for item in answer["items"]] == [seeded]


def test_paginate_leaves_an_answer_without_items_as_it_came(run_cli):
    assert printed(run_cli(["api", "whoami", "--paginate"]))["subject"] == "alice"


def test_paginate_stops_with_an_error_when_the_farm_keeps_sending_the_same_cursor(run_cli, fake_farm):
    fake_farm.override("list_tasks", lambda call: (200, {"items": [], "next_cursor": "same"}))
    result = run_cli(["api", "tasks", "--paginate"])
    assert result.exit_code == 1 and "pagination_loop" in result.stderr
    assert len([r for r in fake_farm.requests if r.path.endswith("/tasks")]) == 2


def test_paginate_is_refused_for_a_method_that_is_not_get(run_cli, fake_farm):
    result = run_cli(["api", "tasks", "-X", "POST", "--paginate"])
    assert result.exit_code == 2 and "GET only" in result.stderr
    assert fake_farm.requests == []


# -- errors and exit codes ----------------------------------------------------------------------------------------


def test_an_error_of_the_farm_keeps_its_code_and_exits_1(run_cli, fake_farm):
    result = run_cli(["api", "tasks/task-nope"])
    assert result.exit_code == 1 and result.stdout == ""
    assert "(not_found)" in result.stderr


def test_an_authentication_failure_exits_4_and_tells_expired_from_invalid(run_cli):
    expired = run_cli(["api", "whoami"], token=EXPIRED)
    invalid = run_cli(["api", "whoami"], token="tok-nobody")
    assert (expired.exit_code, invalid.exit_code) == (4, 4)
    assert "expired" in expired.stderr and "token_expired" in expired.stderr
    assert "token_invalid" in invalid.stderr


def test_without_any_token_it_exits_4_before_sending_anything(run_cli, fake_farm):
    result = run_cli(["api", "whoami"], token=None)
    assert result.exit_code == 4 and fake_farm.requests == []


def test_a_retryable_get_is_retried_and_a_post_is_not(run_cli, fake_farm, sleeps):
    fake_farm.fail_next("whoami", retry_after=2)
    assert printed(run_cli(["api", "whoami"]))["subject"] == "alice"
    assert sleeps == [2]
    fake_farm.fail_next("create_task")
    result = run_cli(["api", "tasks", "-f", "prompt=hello"])
    assert result.exit_code == 1 and "database_busy" in result.stderr
    assert len([r for r in fake_farm.requests if r.method == "POST"]) == 1


def test_an_endpoint_the_farm_does_not_have_is_the_farms_404_not_a_crash(run_cli, fake_farm):
    result = run_cli(["api", "nonsense"])
    assert result.exit_code == 1 and "not_found" in result.stderr
    assert fake_farm.take_violations() == ["unknown operation: GET /farm/api/v1/nonsense"]


# -- the token cannot be redirected --------------------------------------------------------------------------------


@pytest.mark.parametrize("header", ["Authorization: Bearer tok-bob", "authorization:x", "  AUTHORIZATION : y"])
def test_overriding_the_authorization_header_is_refused_before_any_request(run_cli, fake_farm, header):
    result = run_cli(["api", "whoami", "-H", header])
    assert result.exit_code == 2 and "Authorization" in result.stderr
    assert fake_farm.requests == []
    assert "tok-bob" not in result.output


def test_other_headers_are_sent_and_the_token_still_comes_from_the_store(run_cli, fake_farm):
    printed(run_cli(["api", "whoami", "-H", "X-Trace: abc", "-H", "Accept-Language:es"], token=BOB))
    sent = fake_farm.requests[-1]
    assert sent.headers["x-trace"] == "abc" and sent.headers["accept-language"] == "es"
    assert sent.headers["authorization"] == f"Bearer {BOB}"


@pytest.mark.parametrize("header", ["no-colon", ": value", "Bad Name: x", "X-A: line\nbreak"])
def test_a_malformed_header_is_a_usage_error(run_cli, fake_farm, header):
    assert run_cli(["api", "whoami", "-H", header]).exit_code == 2
    assert fake_farm.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/farm/api/v1/whoami",
        "//evil.test/x",
        "../hooks",
        "tasks/../../hooks",
        "tasks/%2e%2e/x",
        "tasks#x",
    ],
)
def test_a_full_url_or_a_path_that_climbs_out_of_the_api_is_refused(run_cli, fake_farm, path):
    result = run_cli(["api", path])
    assert result.exit_code == 2 and result.stdout == ""
    assert fake_farm.requests == []


def test_a_missing_endpoint_is_a_usage_error(run_cli):
    for path in ("", "/", "/farm/api/v1", "/farm/api/v1/"):
        assert run_cli(["api", path]).exit_code == 2


def test_the_token_never_appears_in_the_output_of_a_failed_or_included_call(run_cli):
    for args in (["api", "whoami", "-i"], ["api", "tasks/nope", "-i"], ["api", "whoami", "--input", "-"]):
        result = run_cli(args, input="not json")
        assert ALICE not in result.output


# -- malformed arguments ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["-f", "-F"])
@pytest.mark.parametrize("value", ["novalue", "=x"])
def test_a_field_without_a_key_or_an_equals_sign_is_a_usage_error_that_does_not_echo_it(
    run_cli, fake_farm, flag, value
):
    result = run_cli(["api", "tasks", flag, value])
    assert result.exit_code == 2 and "KEY=VALUE" in result.stderr and value not in result.stderr
    assert fake_farm.requests == []


def test_input_that_is_not_json_is_refused_without_quoting_it(run_cli, fake_farm):
    result = run_cli(["api", "tasks", "--input", "-"], input='{"prompt": "s3cret-sentinel",')
    assert result.exit_code == 2 and "not valid JSON" in result.stderr
    assert "s3cret-sentinel" not in result.output
    assert fake_farm.requests == []


def test_a_file_that_cannot_be_read_is_a_usage_error(run_cli, fake_farm, tmp_path):
    for args in (["--input", str(tmp_path / "missing.json")], ["-F", f"x=@{tmp_path / 'missing.txt'}"]):
        result = run_cli(["api", "tasks", *args])
        assert result.exit_code == 2 and "cannot read" in result.stderr
    assert fake_farm.requests == []


def test_standard_input_cannot_be_both_the_body_and_a_field(run_cli, fake_farm):
    result = run_cli(["api", "tasks", "--input", "-", "-F", "prompt=@-"], input="{}")
    assert result.exit_code == 2 and "standard input" in result.stderr


# -- --include ----------------------------------------------------------------------------------------------------


def test_include_prints_the_status_and_headers_before_the_body(run_cli):
    result = run_cli(["api", "whoami", "--include"])
    head, _, body = result.stdout.partition("\n\n")
    assert head.splitlines()[0] == "HTTP 200 OK"
    assert any(line.lower().startswith("x-agent-farm-token-expires-at:") for line in head.splitlines())
    assert json.loads(body)["subject"] == "alice"


def test_include_on_an_error_puts_the_headers_on_stderr_and_keeps_stdout_clean(run_cli):
    result = run_cli(["api", "tasks/task-nope", "-i"])
    assert result.exit_code == 1 and result.stdout == ""
    assert result.stderr.splitlines()[0] == "HTTP 404 Not Found"


# -- registry -----------------------------------------------------------------------------------------------------


def test_api_is_an_escape_hatch_and_claims_no_operation_in_the_registry():
    import dynfarm.commands.api as module

    assert not any(name.startswith(module.__name__) for names in registry.registered().values() for name in names)
