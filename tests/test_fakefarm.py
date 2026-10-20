"""The fake farm is held to the pinned OpenAPI and behaves like the real one where a client can tell.

Every command test in phases 03-09 stands on the fake, so a fake that is looser than the real farm turns a CLI bug
into a green test. These tests pin the properties the later phases rely on: every operation of the API is served,
anything the contract forbids is refused *and recorded* (so a CLI that sends it fails its own test), identity and
ownership follow `docs/farm-api.md` §3-4 (another tenant or another person is 404; expired is not invalid), errors
carry §15's envelope, and the same model answers over a real socket.
"""

from __future__ import annotations

import httpx
from fakefarm import ADMIN, ALICE, BOB, EXPIRED, HOST, OTHER, READONLY, FakeFarm

API = HOST + "/farm/api/v1"
TASK = {"agent_id": "developer-skills:code-reviewer", "input": "Review my open MRs."}
RECURRENCE = {"cron": "0 8 * * 1-5", "timezone": "Europe/Madrid"}


def call(farm: FakeFarm, method: str, path: str, token: str | None = ALICE, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(transport=farm.transport()) as client:
        return client.request(method, API + path, headers=headers, **kwargs)


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


def new_task(farm: FakeFarm, token: str = ALICE, **extra) -> str:
    response = call(farm, "POST", "/tasks", token, json={**TASK, **extra})
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def new_schedule(farm: FakeFarm, token: str = ALICE) -> str:
    response = call(farm, "POST", "/schedules", token, json={"task": TASK, "recurrence": RECURRENCE})
    assert response.status_code == 201, response.text
    return response.json()["schedule_id"]


# -- the contract -----------------------------------------------------------------------------------------------


def test_every_operation_of_the_pinned_openapi_is_served_and_nothing_else(fake_farm):
    assert set(fake_farm._handlers) == fake_farm.openapi.operation_ids
    assert len(fake_farm.openapi.operation_ids) == 26


def test_an_unknown_operation_is_refused_and_recorded_as_a_violation(fake_farm):
    response = call(fake_farm, "GET", "/nonexistent")
    assert response.status_code == 404
    assert fake_farm.take_violations() == ["unknown operation: GET /farm/api/v1/nonexistent"]


def test_a_known_path_with_the_wrong_method_is_405(fake_farm):
    assert call(fake_farm, "PUT", "/whoami").status_code == 405
    assert len(fake_farm.take_violations()) == 1


def test_a_body_field_the_api_does_not_document_is_refused_and_recorded(fake_farm):
    response = call(fake_farm, "POST", "/tasks", json={**TASK, "colour": "red"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "colour"]
    assert "extra_forbidden" in response.text
    assert len(fake_farm.take_violations()) == 1


def test_a_missing_required_body_field_is_a_422_naming_the_field(fake_farm):
    response = call(fake_farm, "POST", "/credentials", json={"name": "x", "type": "generic"})
    assert response.status_code == 422
    assert response.json()["detail"] == [{"loc": ["body", "value"], "msg": "Field required", "type": "missing"}]
    assert len(fake_farm.take_violations()) == 1


def test_an_undocumented_query_parameter_is_recorded(fake_farm):
    assert call(fake_farm, "GET", "/tasks", params={"colour": "red"}).status_code == 200
    assert "undocumented query parameter 'colour'" in fake_farm.take_violations()[0]


def test_a_query_value_outside_its_bounds_is_a_framework_422(fake_farm):
    response = call(fake_farm, "GET", "/tasks", params={"limit": 500})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]
    assert len(fake_farm.take_violations()) == 1


def test_a_body_that_is_not_json_is_a_400(fake_farm):
    response = call(fake_farm, "POST", "/tasks", content=b"{not json")
    assert response.status_code == 400
    assert error_code(response) == "malformed_json"
    fake_farm.take_violations()


def test_the_tenant_in_a_body_is_a_400_tenant_not_accepted(fake_farm):
    response = call(fake_farm, "POST", "/tasks", json={**TASK, "tenant": "acme"})
    assert response.status_code == 400
    assert error_code(response) == "tenant_not_accepted"
    fake_farm.take_violations()


def test_the_openapi_document_is_served_without_a_token(fake_farm):
    with httpx.Client(transport=fake_farm.transport()) as client:
        response = client.get(HOST + "/openapi.json")
    assert response.status_code == 200
    assert response.json()["openapi"].startswith("3.")


# -- identity ---------------------------------------------------------------------------------------------------


def test_a_missing_or_unknown_token_is_401_token_invalid(fake_farm):
    for token in (None, "tok-nobody"):
        response = call(fake_farm, "GET", "/whoami", token)
        assert response.status_code == 401
        assert error_code(response) == "token_invalid"
        assert 'error_description="invalid"' in response.headers["www-authenticate"]


def test_an_expired_token_is_401_token_expired_with_its_expiry_not_token_invalid(fake_farm):
    response = call(fake_farm, "GET", "/whoami", EXPIRED)
    assert response.status_code == 401
    assert error_code(response) == "token_expired"
    assert response.json()["error"]["details"]["expires_at"] == "2026-09-30T08:00:00Z"
    assert 'error_description="expired"' in response.headers["www-authenticate"]


def test_whoami_tells_who_is_calling_and_every_answer_carries_the_token_expiry(fake_farm):
    response = call(fake_farm, "GET", "/whoami")
    assert response.json()["subject"] == "alice"
    assert response.json()["is_admin"] is False
    assert response.headers["x-agent-farm-token-expires-at"] == "2027-01-01T00:00:00Z"
    assert call(fake_farm, "GET", "/whoami", ADMIN).json()["is_admin"] is True


def test_a_valid_token_without_the_scope_is_403_not_401(fake_farm):
    response = call(fake_farm, "POST", "/tasks", READONLY, json=TASK)
    assert response.status_code == 403
    assert error_code(response) == "insufficient_scope"
    assert call(fake_farm, "GET", "/whoami", READONLY).status_code == 200


# -- ownership --------------------------------------------------------------------------------------------------


def test_a_task_of_another_tenant_or_another_person_is_404_and_an_admin_sees_it(fake_farm):
    task_id = new_task(fake_farm, ALICE)
    assert call(fake_farm, "GET", f"/tasks/{task_id}", OTHER).status_code == 404
    assert call(fake_farm, "GET", f"/tasks/{task_id}", BOB).status_code == 404
    assert call(fake_farm, "DELETE", f"/tasks/{task_id}", BOB).status_code == 404
    assert call(fake_farm, "GET", f"/tasks/{task_id}", ALICE).status_code == 200
    seen_by_admin = call(fake_farm, "GET", f"/tasks/{task_id}", ADMIN)
    assert seen_by_admin.status_code == 200
    assert seen_by_admin.json()["owner"] == "alice"
    assert "owner" not in call(fake_farm, "GET", f"/tasks/{task_id}", ALICE).json()


def test_a_listing_only_contains_what_the_caller_may_see(fake_farm):
    mine, theirs = new_task(fake_farm, ALICE), new_task(fake_farm, BOB)
    assert [t["task_id"] for t in call(fake_farm, "GET", "/tasks", ALICE).json()["items"]] == [mine]
    assert call(fake_farm, "GET", "/tasks", OTHER).json()["items"] == []
    assert {t["task_id"] for t in call(fake_farm, "GET", "/tasks", ADMIN).json()["items"]} == {mine, theirs}
    only_admin = call(fake_farm, "GET", "/tasks", ADMIN, params={"owner": "me"}).json()["items"]
    assert only_admin == []


def test_an_id_that_is_not_a_uuid_or_tries_to_escape_the_route_simply_does_not_exist(fake_farm):
    assert call(fake_farm, "GET", "/tasks/not-a-uuid").status_code == 404
    escaped = httpx.Client(transport=fake_farm.transport()).get(
        API + "/tasks/..%2Fcredentials", headers={"Authorization": f"Bearer {ALICE}"}
    )
    assert escaped.status_code == 404
    assert error_code(escaped) == "task_not_found"


# -- tasks ------------------------------------------------------------------------------------------------------


def test_launching_with_the_same_correlation_id_returns_the_existing_task(fake_farm):
    first = call(fake_farm, "POST", "/tasks", json={**TASK, "correlation_id": "mr-42"})
    again = call(fake_farm, "POST", "/tasks", json={**TASK, "correlation_id": "mr-42"})
    others = call(fake_farm, "POST", "/tasks", BOB, json={**TASK, "correlation_id": "mr-42"})
    assert (first.status_code, again.status_code, others.status_code) == (201, 200, 201)
    assert again.json()["task_id"] == first.json()["task_id"] != others.json()["task_id"]


def test_a_task_needs_an_agent_xor_a_prompt_and_a_known_agent(fake_farm):
    neither = call(fake_farm, "POST", "/tasks", json={"input": "x"})
    both = call(fake_farm, "POST", "/tasks", json={"agent_id": "repo-helper", "prompt": "x"})
    unknown = call(fake_farm, "POST", "/tasks", json={"agent_id": "ghost"})
    assert (error_code(neither), error_code(both)) == ("agent_or_prompt_required",) * 2
    assert (unknown.status_code, error_code(unknown)) == (404, "agent_not_found")
    fake_farm.free_prompt = False
    assert error_code(call(fake_farm, "POST", "/tasks", json={"prompt": "x"})) == "prompt_not_enabled_for_tenant"


def test_the_concurrency_quota_is_a_retryable_429_with_retry_after(fake_farm):
    fake_farm.limits["max_concurrent_tasks"] = 1
    new_task(fake_farm)
    response = call(fake_farm, "POST", "/tasks", json=TASK)
    assert response.status_code == 429
    assert response.json()["error"]["retryable"] is True
    assert response.headers["retry-after"] == "5"


def test_listing_pages_newest_first_with_a_cursor_and_filters_by_state(fake_farm):
    ids = [new_task(fake_farm) for _ in range(3)]
    fake_farm.set_task(ids[0], state="succeeded")
    first = call(fake_farm, "GET", "/tasks", params={"limit": 2}).json()
    assert [t["task_id"] for t in first["items"]] == [ids[2], ids[1]]
    rest = call(fake_farm, "GET", "/tasks", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [t["task_id"] for t in rest["items"]] == [ids[0]]
    assert rest["next_cursor"] is None
    done = call(fake_farm, "GET", "/tasks", params={"state": "succeeded"}).json()["items"]
    assert [t["task_id"] for t in done] == [ids[0]]
    assert error_code(call(fake_farm, "GET", "/tasks", params={"state": "sleeping"})) == "state_invalid"
    fake_farm.take_violations()


def test_cancelling_a_running_task_is_202_and_a_finished_one_is_409(fake_farm):
    task_id = new_task(fake_farm)
    assert call(fake_farm, "DELETE", f"/tasks/{task_id}").status_code == 202
    again = call(fake_farm, "DELETE", f"/tasks/{task_id}")
    assert (again.status_code, error_code(again)) == (409, "task_not_cancellable")


def test_logs_return_the_tail_and_say_when_they_cut_it(fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running", logs=[f"line {n}" for n in range(5)])
    logs = call(fake_farm, "GET", f"/tasks/{task_id}/logs", params={"tail_lines": 2}).json()
    assert logs["lines"] == ["line 3", "line 4"]
    assert logs["truncated"] is True
    assert logs["full_logs"] is None


def test_the_result_of_a_running_task_is_409_and_of_an_expired_one_is_410(fake_farm):
    task_id = new_task(fake_farm)
    assert error_code(call(fake_farm, "GET", f"/tasks/{task_id}/result")) == "task_not_terminal"
    fake_farm.expire_result(task_id)
    assert call(fake_farm, "GET", f"/tasks/{task_id}/result").status_code == 410


def test_a_succeeded_task_has_a_result_whose_presigned_url_needs_no_token(fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="succeeded")
    result = call(fake_farm, "GET", f"/tasks/{task_id}/result").json()
    assert result["summary"]
    document = httpx.Client(transport=fake_farm.transport()).get(result["result"]["url"])
    assert document.status_code == 200
    assert document.json()["task_id"] == task_id
    assert "authorization" not in fake_farm.requests[-1].headers


# -- agents, plugins, callback key ------------------------------------------------------------------------------


def test_the_catalogue_lists_platform_agents_and_plugins(fake_farm):
    slugs = [a["slug"] for a in call(fake_farm, "GET", "/agents").json()["items"]]
    assert "developer-skills:code-reviewer" in slugs
    assert call(fake_farm, "GET", "/agents/repo-helper").json()["scope"] == "platform"
    assert call(fake_farm, "GET", "/plugins").json()["items"][0]["name"] == "developer-skills"
    assert call(fake_farm, "GET", "/callback-key").json()["keys"][0]["alg"] == "EdDSA"


def test_a_personal_agent_is_private_a_team_agent_needs_the_admin_scope_and_a_slug_is_unique(fake_farm):
    created = call(fake_farm, "POST", "/agents", json={"slug": "mine", "scope": "personal", "description": "d"})
    assert created.status_code == 201
    assert created.json()["owner"] == "alice"
    assert call(fake_farm, "GET", "/agents/mine", BOB).status_code == 404
    assert (
        error_code(call(fake_farm, "POST", "/agents", json={"slug": "mine", "scope": "personal"})) == "agent_slug_taken"
    )
    assert call(fake_farm, "POST", "/agents", json={"slug": "t", "scope": "team"}).status_code == 403
    assert call(fake_farm, "POST", "/agents", ADMIN, json={"slug": "t", "scope": "team"}).status_code == 201
    assert "t" in [a["slug"] for a in call(fake_farm, "GET", "/agents", BOB).json()["items"]]


def test_editing_an_agent_changes_only_what_is_sent_and_deleting_it_is_204(fake_farm):
    call(
        fake_farm,
        "POST",
        "/agents",
        json={"slug": "mine", "scope": "personal", "description": "old", "instructions": "i"},
    )
    patched = call(fake_farm, "PATCH", "/agents/mine", json={"description": "new"}).json()
    assert (patched["description"], patched["instructions"]) == ("new", "i")
    assert call(fake_farm, "DELETE", "/agents/mine").status_code == 204
    assert call(fake_farm, "GET", "/agents/mine").status_code == 404


def test_an_agent_a_live_schedule_launches_cannot_be_deleted(fake_farm):
    call(fake_farm, "POST", "/agents", json={"slug": "mine", "scope": "personal"})
    call(fake_farm, "POST", "/schedules", json={"task": {"agent_id": "mine"}, "recurrence": RECURRENCE})
    assert error_code(call(fake_farm, "DELETE", "/agents/mine")) == "agent_in_use_by_schedule"


# -- schedules --------------------------------------------------------------------------------------------------


def test_a_schedule_is_validated_for_its_cron_timezone_and_task(fake_farm):
    bad_cron = call(fake_farm, "POST", "/schedules", json={"task": TASK, "recurrence": {**RECURRENCE, "cron": "* * *"}})
    bad_zone = call(
        fake_farm, "POST", "/schedules", json={"task": TASK, "recurrence": {**RECURRENCE, "timezone": "Mars/Base"}}
    )
    bad_task = call(
        fake_farm,
        "POST",
        "/schedules",
        json={"task": {"prompt": "x", "agent_id": "repo-helper"}, "recurrence": RECURRENCE},
    )
    assert error_code(bad_cron) == "cron_invalid"
    assert error_code(bad_zone) == "timezone_invalid"
    assert error_code(bad_task) == "agent_or_prompt_required"
    typo = call(fake_farm, "POST", "/schedules", json={"task": {**TASK, "colour": "red"}, "recurrence": RECURRENCE})
    assert typo.status_code == 422
    assert len(fake_farm.take_violations()) == 1


def test_a_schedule_can_be_paused_resumed_fired_and_deleted_by_its_owner_only(fake_farm):
    schedule_id = new_schedule(fake_farm)
    assert call(fake_farm, "GET", f"/schedules/{schedule_id}", BOB).status_code == 404
    assert call(fake_farm, "POST", f"/schedules/{schedule_id}/pause").json()["state"] == "paused"
    not_active = call(fake_farm, "POST", f"/schedules/{schedule_id}/fire")
    assert (not_active.status_code, error_code(not_active)) == (409, "schedule_not_active")
    assert call(fake_farm, "POST", f"/schedules/{schedule_id}/resume").json()["state"] == "active"
    fired = call(fake_farm, "POST", f"/schedules/{schedule_id}/fire")
    assert fired.status_code == 201
    assert (fired.json()["origin"], fired.json()["trigger"]) == ("schedule", "manual")
    assert fired.json()["correlation_id"].startswith("sched-manual-")
    overlap = call(fake_farm, "POST", f"/schedules/{schedule_id}/fire")
    assert error_code(overlap) == "schedule_overlap"
    listed = call(fake_farm, "GET", f"/schedules/{schedule_id}/tasks").json()["items"]
    assert [t["task_id"] for t in listed] == [fired.json()["task_id"]]
    assert call(fake_farm, "DELETE", f"/schedules/{schedule_id}").status_code == 204
    assert call(fake_farm, "GET", f"/schedules/{schedule_id}").status_code == 404


# -- credentials ------------------------------------------------------------------------------------------------


def test_a_credential_is_written_then_rotated_and_its_value_is_never_returned(fake_farm):
    body = {"name": "git-readonly", "type": "git_token", "value": "SENTINEL-VALUE-1", "description": "first"}
    created = call(fake_farm, "POST", "/credentials", json=body)
    rotated = call(fake_farm, "POST", "/credentials", json={**body, "value": "SENTINEL-VALUE-2", "description": ""})
    assert (created.status_code, rotated.status_code) == (201, 200)
    assert created.json()["fingerprint"] != rotated.json()["fingerprint"]
    assert rotated.json()["description"] is None
    shown = (
        call(fake_farm, "GET", "/credentials").text
        + call(fake_farm, "GET", "/credentials/git-readonly").text
        + rotated.text
    )
    assert "SENTINEL" not in shown
    assert list(fake_farm.credential_values.values()) == ["SENTINEL-VALUE-2"]


def test_credentials_are_personal_unless_shared_and_shared_ones_need_the_admin_scope(fake_farm):
    body = {"name": "web", "type": "generic", "value": "secret-value"}
    call(fake_farm, "POST", "/credentials", json=body)
    assert call(fake_farm, "GET", "/credentials/web", BOB).status_code == 404
    assert call(fake_farm, "GET", "/credentials", BOB).json()["items"] == []
    assert call(fake_farm, "POST", "/credentials", json={**body, "scope": "shared"}).status_code == 403
    assert call(fake_farm, "POST", "/credentials", ADMIN, json={**body, "scope": "shared"}).status_code == 201
    shared = call(fake_farm, "GET", "/credentials", BOB, params={"scope": "shared"}).json()["items"]
    assert [c["name"] for c in shared] == ["web"]
    assert call(fake_farm, "DELETE", "/credentials/web", BOB, params={"scope": "shared"}).status_code == 403


def test_deleting_a_credential_is_204_then_404_and_a_bad_mcp_url_is_refused_without_echoing_it(fake_farm):
    call(fake_farm, "POST", "/credentials", json={"name": "gone", "type": "generic", "value": "v"})
    assert call(fake_farm, "DELETE", "/credentials/gone").status_code == 204
    missing = call(fake_farm, "GET", "/credentials/gone")
    assert (missing.status_code, error_code(missing)) == (404, "credentials_not_found")
    bad = call(
        fake_farm,
        "POST",
        "/credentials",
        json={"name": "m", "type": "mcp_url", "value": "http://plain.example/SENTINEL"},
    )
    assert error_code(bad) == "credential_value_invalid"
    assert "SENTINEL" not in bad.text


def test_the_recording_shows_what_crossed_the_wire(fake_farm):
    call(fake_farm, "POST", "/credentials", json={"name": "n", "type": "generic", "value": "SENTINEL-WIRE"})
    call(fake_farm, "GET", "/credentials")
    assert [r.operation_id for r in fake_farm.requests] == ["write_credential", "list_credentials"]
    assert [r.status for r in fake_farm.requests] == [201, 200]
    bodies = [r.body for r in fake_farm.requests if b"SENTINEL-WIRE" in r.body]
    assert len(bodies) == 1
    assert "SENTINEL-WIRE" in fake_farm.recorded_text()


# -- failure injection and the real socket ----------------------------------------------------------------------


def test_an_injected_failure_answers_once_with_the_given_error_and_then_recovers(fake_farm):
    fake_farm.fail_next("whoami", status=503, code="cluster_unavailable", retry_after=2)
    failed = call(fake_farm, "GET", "/whoami")
    assert (failed.status_code, error_code(failed), failed.headers["retry-after"]) == (503, "cluster_unavailable", "2")
    assert failed.json()["error"]["retryable"] is True
    assert call(fake_farm, "GET", "/whoami").status_code == 200


def test_the_same_farm_answers_over_a_real_socket(fake_farm):
    with fake_farm.serve() as url:
        assert url.startswith("http://127.0.0.1:")
        who = httpx.get(url + "/farm/api/v1/whoami", headers={"Authorization": f"Bearer {ALICE}"})
        assert who.status_code == 200
        assert who.json()["subject"] == "alice"
        denied = httpx.get(url + "/farm/api/v1/whoami", headers={"Authorization": f"Bearer {EXPIRED}"})
        assert (denied.status_code, error_code(denied)) == (401, "token_expired")
        created = httpx.post(url + "/farm/api/v1/tasks", json=TASK, headers={"Authorization": f"Bearer {ALICE}"})
        assert created.status_code == 201
        assert httpx.get(url + "/openapi.json").status_code == 200
    assert fake_farm.base_url == HOST
