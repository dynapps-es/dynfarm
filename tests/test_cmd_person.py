"""`dynfarm person`: finding people, and the one command that cannot be undone.

Why these tests exist: `person offboard` removes a person's credentials and schedules and blocks them, so what the
tests pin is the safety around it: a dry run unless `--apply`, the subject typed to go on, nothing sent when the
confirmation is wrong, and the farm's half-way `503` shown with what was already done. The fake farm validates every
request against the pinned OpenAPI, so a body the API would refuse fails the test even when nobody looked.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fakefarm import ADMIN, ALICE, OTHER, PLATFORM, TENANT

import dynfarm.commands

API = "/farm/api/v1/people"


@pytest.fixture
def terminal(monkeypatch):
    def choose(tty):
        monkeypatch.setattr(dynfarm.commands, "is_interactive", lambda: tty)

    return choose


def sent(fake_farm, operation_id):
    return [r for r in fake_farm.requests if r.operation_id == operation_id]


def body_of(request) -> dict:
    return json.loads(request.body)


def person_id(fake_farm, subject, tenant=TENANT) -> str:
    return fake_farm._person(tenant, subject)["person_id"]


def rows(result) -> list[list[str]]:
    assert result.exit_code == 0, result.output
    return [line.split("\t") for line in result.stdout.splitlines()]


def give_alice_things(fake_farm):
    """Arrange: a running task, a personal credential and a schedule for alice; leaves the request log empty."""
    task_id = fake_farm.seed_task(ALICE, state="running")
    with httpx.Client(transport=fake_farm.transport(), headers={"Authorization": f"Bearer {ALICE}"}) as client:
        saved = client.post(
            fake_farm.base_url + "/farm/api/v1/credentials",
            json={"name": "git-readonly", "type": "git_token", "value": "arranged"},
        )
        assert saved.status_code == 201, saved.text
    fake_farm.requests.clear()
    return task_id


# -- list and view ------------------------------------------------------------------------------------------------


def test_list_shows_the_people_of_the_tenant_to_an_admin(run_cli, fake_farm):
    listed = rows(run_cli(["person", "list"], token=ADMIN))
    assert [row[1] for row in listed] == ["alice", "bob", "ci-bot", "dana", "erin", "pat"]
    assert listed[0][0] == person_id(fake_farm, "alice") and listed[0][2:4] == ["person", TENANT]
    assert sent(fake_farm, "list_people")[0].query == {}


def test_list_filters_by_kind_and_by_offboarded(run_cli, fake_farm):
    assert [row[1] for row in rows(run_cli(["person", "list", "--kind", "service"], token=ADMIN))] == ["ci-bot"]
    assert sent(fake_farm, "list_people")[0].query == {"kind": ["service"]}
    run_cli(["person", "offboard", person_id(fake_farm, "bob"), "--apply", "--yes"], token=ADMIN)
    result = run_cli(["person", "list", "--offboarded"], token=ADMIN)
    assert [row[1] for row in rows(result)] == ["bob"]
    assert sent(fake_farm, "list_people")[-1].query == {"offboarded": ["true"]}
    active = rows(run_cli(["person", "list", "--active"], token=ADMIN))
    assert "bob" not in [row[1] for row in active] and len(active) == 5
    assert sent(fake_farm, "list_people")[-1].query == {"offboarded": ["false"]}


def test_list_tenant_sends_the_parameter_and_reads_other_tenants_with_a_platform_key(run_cli, fake_farm):
    result = run_cli(["person", "list", "--tenant", "*"], token=PLATFORM)
    assert {row[3] for row in rows(result)} == {TENANT, "acme"}
    assert sent(fake_farm, "list_people")[0].query == {"tenant": ["*"]}


def test_list_tenant_without_a_platform_key_says_what_the_key_needs(run_cli):
    result = run_cli(["person", "list", "--tenant", "acme"], token=ADMIN)
    assert result.exit_code == 1
    assert "insufficient_scope" in result.stderr and "farm:platform:read" in result.stderr


def test_list_without_admin_is_refused(run_cli):
    result = run_cli(["person", "list"])
    assert result.exit_code == 1 and "farm:tenant:admin" in result.stderr


def test_list_json_is_the_farm_s_answer(run_cli):
    answer = json.loads(run_cli(["person", "list", "--json"], token=ADMIN).stdout)
    assert answer["items"][0]["subject"] == "alice" and "person_id" in answer["items"][0]


def test_view_shows_one_person_and_an_unknown_id_is_not_found(run_cli, fake_farm):
    result = run_cli(["person", "view", person_id(fake_farm, "alice")], token=ADMIN)
    assert result.exit_code == 0 and "subject:" in result.stdout and "alice" in result.stdout
    missing = run_cli(["person", "view", "no-such-id"], token=ADMIN)
    assert missing.exit_code == 1 and "not_found" in missing.stderr


# -- offboard: the dry run ----------------------------------------------------------------------------------------


def test_offboard_is_a_dry_run_that_names_what_would_go_and_changes_nothing(run_cli, fake_farm):
    task_id = give_alice_things(fake_farm)
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice")], token=ADMIN)
    assert result.exit_code == 0, result.output
    assert "Would offboard alice" in result.stdout and task_id in result.stdout and "git-readonly" in result.stdout
    assert "passes to dana" in result.stdout
    assert "Nothing was changed. Run again with --apply to offboard alice" in result.stdout
    assert body_of(sent(fake_farm, "offboard_person")[0]) == {"dry_run": True}
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is None
    assert run_cli(["whoami"]).exit_code == 0


def test_offboard_transfer_to_goes_in_the_body_and_a_bad_one_is_explained(run_cli, fake_farm):
    run_cli(
        ["person", "offboard", person_id(fake_farm, "alice"), "--transfer-to", person_id(fake_farm, "bob")], token=ADMIN
    )
    assert body_of(sent(fake_farm, "offboard_person")[0]) == {
        "dry_run": True,
        "transfer_to": person_id(fake_farm, "bob"),
    }
    service = run_cli(
        ["person", "offboard", person_id(fake_farm, "alice"), "--transfer-to", person_id(fake_farm, "ci-bot")],
        token=ADMIN,
    )
    assert service.exit_code == 1 and "transfer_to_invalid" in service.stderr
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is None


def test_offboard_json_prints_the_report_verbatim(run_cli, fake_farm):
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--json"], token=ADMIN)
    report = json.loads(result.stdout)
    assert report["dry_run"] is True and report["person"]["subject"] == "alice"


def test_offboard_yourself_is_refused_by_the_farm(run_cli, fake_farm):
    result = run_cli(["person", "offboard", person_id(fake_farm, "dana")], token=ADMIN)
    assert result.exit_code == 1 and "cannot_offboard_self" in result.stderr


# -- offboard: --apply --------------------------------------------------------------------------------------------


def test_apply_without_a_terminal_and_without_yes_does_not_call_the_farm_to_change_anything(
    run_cli, fake_farm, terminal
):
    terminal(False)
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply"], token=ADMIN)
    assert result.exit_code == 2 and "pass --yes" in result.stderr
    assert all(body_of(r)["dry_run"] is True for r in sent(fake_farm, "offboard_person"))
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is None


def test_apply_with_the_subject_typed_offboards_with_dry_run_false(run_cli, fake_farm, terminal):
    terminal(True)
    task_id = give_alice_things(fake_farm)
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply"], input="alice\n", token=ADMIN)
    assert result.exit_code == 0, result.output
    assert [body_of(r)["dry_run"] for r in sent(fake_farm, "offboard_person")] == [True, False]
    assert "Offboarded alice" in result.stdout and task_id in result.stdout
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is not None
    assert fake_farm.credential_values == {}
    blocked = run_cli(["whoami"])
    assert blocked.exit_code == 1 and "person_offboarded" in blocked.stderr


def test_apply_with_another_text_cancels_and_changes_nothing(run_cli, fake_farm, terminal):
    terminal(True)
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply"], input="bob\n", token=ADMIN)
    assert result.exit_code == 1 and "cancelled; nothing was changed" in result.stderr
    assert [body_of(r)["dry_run"] for r in sent(fake_farm, "offboard_person")] == [True]
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is None


def test_apply_with_yes_does_not_ask_and_json_prints_only_the_final_report(run_cli, fake_farm):
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply", "--yes", "--json"], token=ADMIN)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"] is False
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is not None


def test_a_503_half_way_shows_what_was_done_and_exits_non_zero(run_cli, fake_farm):
    original = fake_farm.override("offboard_person", lambda call: (200, {}))

    def handler(call):
        if call.body.get("dry_run", True):
            return original(call)
        from fakefarm import ApiProblem

        raise ApiProblem(
            503,
            "cluster_unavailable",
            "the cluster does not answer",
            retryable=True,
            details={"done": {"marked": True, "tasks": 1}},
        )

    fake_farm.override("offboard_person", handler)
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply", "--yes"], token=ADMIN)
    assert result.exit_code == 1
    assert "Already done" in result.stderr and "marked=True" in result.stderr and "tasks=1" in result.stderr
    assert "run the same command again to finish it" in result.stderr


def test_a_403_from_a_key_that_is_not_admin_is_shown(run_cli, fake_farm):
    result = run_cli(["person", "offboard", person_id(fake_farm, "bob")], token=PLATFORM)
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


def test_another_tenants_person_is_not_found(run_cli, fake_farm):
    result = run_cli(["person", "offboard", person_id(fake_farm, "carol", "acme")], token=ADMIN)
    assert result.exit_code == 1 and "not_found" in result.stderr
    assert OTHER  # the other tenant's key exists; its person is simply not ours


# -- readmit ------------------------------------------------------------------------------------------------------


def test_readmit_without_confirmation_does_not_call(run_cli, fake_farm, terminal):
    terminal(False)
    result = run_cli(["person", "readmit", person_id(fake_farm, "alice")], token=ADMIN)
    assert result.exit_code == 2 and "pass --yes" in result.stderr
    assert sent(fake_farm, "readmit_person") == []


def test_readmit_asks_and_a_no_changes_nothing(run_cli, fake_farm, terminal):
    terminal(True)
    run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply", "--yes"], token=ADMIN)
    result = run_cli(["person", "readmit", person_id(fake_farm, "alice")], input="n\n", token=ADMIN)
    assert result.exit_code == 1 and "cancelled" in result.stderr
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is not None


def test_readmit_with_yes_lets_the_person_back_in(run_cli, fake_farm):
    run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply", "--yes"], token=ADMIN)
    assert run_cli(["whoami"]).exit_code == 1
    result = run_cli(["person", "readmit", person_id(fake_farm, "alice"), "--yes"], token=ADMIN)
    assert result.exit_code == 0 and "readmitted alice" in result.stdout
    assert run_cli(["whoami"]).exit_code == 0


def test_readmit_of_someone_not_offboarded_is_explained_and_json_is_the_person(run_cli, fake_farm):
    result = run_cli(["person", "readmit", person_id(fake_farm, "alice"), "--yes"], token=ADMIN)
    assert result.exit_code == 1 and "person_not_offboarded" in result.stderr
    run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply", "--yes"], token=ADMIN)
    again = run_cli(["person", "readmit", person_id(fake_farm, "alice"), "--yes", "--json"], token=ADMIN)
    assert json.loads(again.stdout)["offboarded_at"] is None


def test_the_commands_declare_their_operations():
    from dynfarm import registry

    registry.load_all_commands()
    declared = registry.registered()
    for operation_id, command in (
        ("list_people", "person.list_"),
        ("get_person", "person.view"),
        ("offboard_person", "person.offboard"),
        ("readmit_person", "person.readmit"),
    ):
        assert any(name.endswith(command) for name in declared[operation_id]), operation_id


# -- the report with everything in it -----------------------------------------------------------------------------


def seed_full_report(fake_farm):
    give_alice_things(fake_farm)
    fake_farm.seed_extras(
        "alice",
        team_agents=["alice-etl", "alice-docs"],
        shared_credentials=["shared-s3"],
        channel_links=[{"channel": "teams", "count": 2}, {"channel": "slack", "count": 1}],
        channel_people=[{"channel": "teams", "active": True}],
        link_codes=1,
        upload_tickets=2,
    )


def test_the_dry_run_report_paints_the_handover_the_chat_links_and_the_channel_warning(run_cli, fake_farm):
    seed_full_report(fake_farm)
    result = run_cli(
        ["person", "offboard", person_id(fake_farm, "alice"), "--transfer-to", person_id(fake_farm, "bob")], token=ADMIN
    )
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Would offboard alice" in out
    assert "passes to bob (person):" in out
    assert "team agents: alice-etl, alice-docs" in out and "shared credentials: shared-s3" in out
    assert "chat links: 3" in out and "- teams: 2" in out and "- slack: 1" in out
    assert "link codes: 1" in out and "upload tickets: 2" in out
    assert "channel list: teams (active: True)" in out
    assert "warning: the channel's list of e-mails still names the person" in out
    assert "Nothing was changed" in out
    assert fake_farm._person(TENANT, "alice")["offboarded_at"] is None


def test_the_applied_report_paints_the_same_and_says_offboarded(run_cli, fake_farm):
    seed_full_report(fake_farm)
    result = run_cli(["person", "offboard", person_id(fake_farm, "alice"), "--apply", "--yes"], token=ADMIN)
    assert result.exit_code == 0, result.output
    final = result.stdout.split("Offboarded alice", 1)[1]
    assert "passes to dana (person):" in final and "team agents: alice-etl, alice-docs" in final
    assert "chat links: 3" in final and "channel list: teams (active: True)" in final
    assert "warning: the channel's list of e-mails" in final


def test_a_service_person_shows_the_integration_warning(run_cli, fake_farm):
    result = run_cli(["person", "offboard", person_id(fake_farm, "ci-bot")], token=ADMIN)
    assert "Would offboard ci-bot (service" in result.stdout
    assert "warning: blocking this key stops every integration" in result.stdout
