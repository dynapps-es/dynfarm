"""`dynfarm agent`: see the catalogue and manage your own and your team's agents, as a person or a pipeline does.

Why these tests exist: an agent is the definition every later task runs on, and a team agent is shared by the whole
tenant, so the commands that write one must send exactly what the flags and the file describe (and only that on an
`edit`, because a PATCH that resends untouched fields would overwrite somebody else's change), never pick the shared
level for the person, and ask before deleting. The three levels are the farm's to police: a platform agent is
read-only, a team agent needs a tenant admin, and another person's personal agent or another tenant's is a 404, not a
403 — the commands must show that answer with its code and not turn it into a crash or a retry. The fake farm
validates every request against the pinned OpenAPI, so a field `AgentCreate`/`AgentUpdate` does not know fails here
even when a test does not look.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fakefarm import ADMIN, ALICE, BOB, OTHER, READONLY

import dynfarm.commands
import dynfarm.output as output
from dynfarm import registry

API = "/farm/api/v1/agents"
REVIEWER = "example-plugin:code-reviewer"


def body_of(request) -> dict:
    return json.loads(request.body)


def sent(fake_farm, operation_id):
    return [r for r in fake_farm.requests if r.operation_id == operation_id]


def printed(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def make(fake_farm, token, slug, scope="personal", **fields):
    """Arrange: create an agent through the API as `token`, without leaving the call in `fake_farm.requests`."""
    with httpx.Client(transport=fake_farm.transport(), headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.post(fake_farm.base_url + API, json={"slug": slug, "scope": scope, **fields})
    assert response.status_code == 201, response.text
    fake_farm.requests.clear()


def rows(result) -> list[list[str]]:
    """The tab-separated rows `agent list` prints when stdout is a pipe."""
    assert result.exit_code == 0, result.output
    return [line.split("\t") for line in result.stdout.splitlines()]


@pytest.fixture
def terminal(monkeypatch):
    """`terminal(True)`: a person is at a terminal (so `delete` may ask), `terminal(False)`: a script."""

    def choose(tty):
        monkeypatch.setattr(dynfarm.commands, "is_interactive", lambda: tty)

    return choose


# -- list ---------------------------------------------------------------------------------------------------------


def test_list_shows_the_platforms_agents_your_teams_and_your_own_but_not_other_peoples(run_cli, fake_farm):
    make(fake_farm, ALICE, "alice-mine", description="Alice's")
    make(fake_farm, BOB, "bob-mine")
    make(fake_farm, ADMIN, "shared", scope="team", description="For everybody")
    listed = rows(run_cli(["agent", "list"]))
    assert {(row[0], row[1]) for row in listed} == {
        ("alice-mine", "personal"),
        ("shared", "team"),
        (REVIEWER, "platform"),
        ("repo-helper", "platform"),
    }
    assert "bob-mine" not in {row[0] for row in listed}


def test_list_prints_slug_scope_owner_and_description_per_row(run_cli, fake_farm):
    make(fake_farm, ALICE, "alice-mine", description="Alice's helper")
    row = next(r for r in rows(run_cli(["agent", "list"])) if r[0] == "alice-mine")
    assert row == ["alice-mine", "personal", "alice", "Alice's helper"]


def test_list_leaves_the_owner_of_a_team_or_platform_agent_empty(run_cli, fake_farm):
    row = next(r for r in rows(run_cli(["agent", "list"])) if r[0] == "repo-helper")
    assert row[:3] == ["repo-helper", "platform", ""]


def test_list_sends_scope_and_owner_to_the_farm_and_shows_what_it_returns(run_cli, fake_farm):
    make(fake_farm, ALICE, "alice-mine")
    make(fake_farm, ADMIN, "shared", scope="team")
    result = run_cli(["agent", "list", "--scope", "personal", "--owner", "me"])
    assert [r[0] for r in rows(result)] == ["alice-mine"]
    assert sent(fake_farm, "list_agents")[0].query == {"scope": ["personal"], "owner": ["me"]}


def test_list_sends_no_query_when_no_filter_is_given(run_cli, fake_farm):
    run_cli(["agent", "list"])
    assert sent(fake_farm, "list_agents")[0].query == {}


def test_list_refuses_a_scope_that_is_not_a_level_before_any_request(run_cli, fake_farm):
    result = run_cli(["agent", "list", "--scope", "everyone"])
    assert result.exit_code == 2 and fake_farm.requests == []


def test_list_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    make(fake_farm, ALICE, "alice-mine")
    assert printed(run_cli(["agent", "list", "--json"])) == printed(run_cli(["api", "agents"]))


def test_list_on_a_terminal_prints_a_table_with_headers_and_counts_the_agents(run_cli, fake_farm, monkeypatch):
    monkeypatch.setattr(output, "_is_tty", lambda stream: True)
    monkeypatch.setenv("NO_COLOR", "1")
    result = run_cli(["agent", "list"])
    assert result.stdout.splitlines()[0].split() == ["SLUG", "SCOPE", "OWNER", "DESCRIPTION"]
    assert result.stderr == "2 agents\n"


def test_list_says_nothing_about_a_count_when_piped_or_with_json(run_cli, fake_farm, monkeypatch):
    assert run_cli(["agent", "list"]).stderr == ""
    monkeypatch.setattr(output, "_is_tty", lambda stream: True)
    assert run_cli(["agent", "list", "--json"]).stderr == ""


def test_list_of_nothing_on_a_terminal_says_so_instead_of_counting_zero(run_cli, fake_farm, monkeypatch):
    monkeypatch.setattr(output, "_is_tty", lambda stream: True)
    result = run_cli(["agent", "list", "--scope", "team"])
    assert result.exit_code == 0 and result.stderr == "no results\n"


def test_list_does_not_show_another_tenants_agents(run_cli, fake_farm):
    make(fake_farm, ADMIN, "ours", scope="team")
    make(fake_farm, OTHER, "theirs")
    assert "ours" not in {r[0] for r in rows(run_cli(["agent", "list"], token=OTHER))}
    assert "theirs" not in {r[0] for r in rows(run_cli(["agent", "list"]))}


# -- view ---------------------------------------------------------------------------------------------------------


def test_view_shows_what_the_agent_is_for_what_it_needs_and_what_it_may_touch(run_cli, fake_farm):
    make(
        fake_farm,
        ALICE,
        "web-manager",
        description="Keeps the website up to date",
        instructions="You maintain the website.\nAnswer in Spanish.",
        plugin_agent=REVIEWER,
        requires_workspace=True,
        credentials=["git-readonly"],
        egress=["github.com"],
        env_credentials={"WEB_API_KEY": "web-api"},
        mcp_servers=[{"name": "docs", "credential": "mcp-docs"}],
        limits={"timeout_s": 600},
    )
    out = run_cli(["agent", "view", "web-manager"]).stdout
    for expected in (
        "slug:",
        "web-manager",
        "scope:",
        "personal",
        "owner:",
        "alice",
        "description:",
        "Keeps the website up to date",
        "plugin agent:",
        REVIEWER,
        "requires workspace:",
        "yes",
        "credentials:",
        "git-readonly",
        "egress:",
        "github.com",
        "env credentials:",
        "WEB_API_KEY=web-api",
        "mcp servers:",
        "docs (credential mcp-docs)",
        "limits:",
        "timeout_s=600",
        "instructions:\n  You maintain the website.\n  Answer in Spanish.",
    ):
        assert expected in out, expected


def test_view_leaves_out_what_the_agent_does_not_have(run_cli, fake_farm):
    out = run_cli(["agent", "view", "repo-helper"]).stdout
    assert "instructions:" not in out and "image:" not in out and "owner:" not in out
    assert "scope:" in out and "platform" in out


def test_view_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine", instructions="x")
    assert printed(run_cli(["agent", "view", "mine", "--json"])) == printed(run_cli(["api", "agents/mine"]))


def test_view_of_somebody_elses_personal_agent_is_a_404_with_its_code(run_cli, fake_farm):
    make(fake_farm, BOB, "bobs")
    result = run_cli(["agent", "view", "bobs"])
    assert result.exit_code == 1 and "agent_not_found" in result.stderr


def test_view_of_another_tenants_team_agent_is_a_404(run_cli, fake_farm):
    make(fake_farm, ADMIN, "ours", scope="team")
    assert run_cli(["agent", "view", "ours"], token=OTHER).exit_code == 1


def test_view_escapes_the_slug_so_it_cannot_reach_another_route(run_cli, fake_farm):
    result = run_cli(["agent", "view", "../tasks"])
    assert result.exit_code == 1
    assert [(r.method, r.target) for r in fake_farm.requests] == [("GET", f"{API}/..%2Ftasks")]


def test_view_strips_terminal_control_characters_from_what_an_agent_says(run_cli, fake_farm):
    make(fake_farm, ADMIN, "sneaky", scope="team", description="hi \x1b[2Jthere", instructions="a\x1b]0;x\x07b")
    out = run_cli(["agent", "view", "sneaky"]).stdout
    assert "\x1b" not in out and "\x07" not in out


# -- create -------------------------------------------------------------------------------------------------------


def test_create_from_flags_sends_exactly_those_fields_and_prints_the_new_agent(run_cli, fake_farm):
    result = run_cli(
        [
            "agent",
            "create",
            "web-manager",
            "--scope",
            "personal",
            "--description",
            "Keeps the site up",
            "--instructions",
            "You maintain the site.",
            "--plugin-agent",
            REVIEWER,
            "--requires-workspace",
        ]
    )
    assert body_of(sent(fake_farm, "create_agent")[0]) == {
        "slug": "web-manager",
        "scope": "personal",
        "description": "Keeps the site up",
        "instructions": "You maintain the site.",
        "plugin_agent": REVIEWER,
        "requires_workspace": True,
    }
    assert rows(result) == [["web-manager", "personal", "alice", "Keeps the site up"]]
    assert "dynfarm task run web-manager" in result.stderr


def test_create_json_is_the_farms_answer_and_leaves_the_hint_out(run_cli, fake_farm):
    result = run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions", "x", "--json"])
    assert printed(result)["slug"] == "mine" and result.stderr == ""


def test_create_sends_only_the_flags_that_were_given(run_cli, fake_farm):
    run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions", "x"])
    assert body_of(sent(fake_farm, "create_agent")[0]) == {"slug": "mine", "scope": "personal", "instructions": "x"}


def test_create_can_say_an_agent_does_not_need_a_workspace(run_cli, fake_farm):
    run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions", "x", "--no-requires-workspace"])
    assert body_of(sent(fake_farm, "create_agent")[0])["requires_workspace"] is False


def test_create_without_a_scope_is_refused_before_any_request_and_names_both_levels(run_cli, fake_farm):
    result = run_cli(["agent", "create", "mine", "--instructions", "x"])
    assert result.exit_code == 2 and fake_farm.requests == []
    assert "--scope personal" in result.stderr and "--scope team" in result.stderr


def test_create_never_picks_the_team_level_for_you(run_cli, fake_farm):
    run_cli(["agent", "create", "mine", "--instructions", "x"], token=ADMIN)
    assert fake_farm.requests == []


def test_create_refuses_a_scope_the_api_has_no_such_level_for(run_cli, fake_farm):
    result = run_cli(["agent", "create", "mine", "--scope", "platform", "--instructions", "x"])
    assert result.exit_code == 2 and fake_farm.requests == []


def test_create_takes_everything_without_a_flag_from_a_file(run_cli, fake_farm, tmp_path):
    document = {
        "scope": "personal",
        "instructions": "Do the ETL.",
        "credentials": ["git-readonly"],
        "egress": ["github.com"],
        "limits": {"timeout_s": 600},
        "mcp_servers": [{"name": "docs", "credential": "mcp-docs"}],
    }
    path = tmp_path / "etl.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    result = run_cli(["agent", "create", "etl", "--from-file", str(path)])
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "create_agent")[0]) == {"slug": "etl", **document}


def test_create_flags_override_the_same_fields_of_the_file(run_cli, fake_farm, tmp_path):
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps({"scope": "personal", "description": "from file", "instructions": "from file"}), encoding="utf-8"
    )
    run_cli(
        ["agent", "create", "mine", "--from-file", str(path), "--description", "from flag", "--scope", "team"],
        token=ADMIN,
    )
    assert body_of(sent(fake_farm, "create_agent")[0]) == {
        "slug": "mine",
        "scope": "team",
        "description": "from flag",
        "instructions": "from file",
    }


def test_create_reads_the_whole_agent_from_standard_input_with_a_dash(run_cli, fake_farm):
    document = json.dumps({"scope": "personal", "instructions": "x"})
    assert run_cli(["agent", "create", "piped", "--from-file", "-"], input=document).exit_code == 0
    assert body_of(sent(fake_farm, "create_agent")[0])["slug"] == "piped"


def test_create_reads_the_instructions_from_a_file_without_its_final_newline(run_cli, fake_farm, tmp_path):
    path = tmp_path / "instructions.md"
    path.write_text("Line one.\nLine two.\n", encoding="utf-8")
    run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions-file", str(path)])
    assert body_of(sent(fake_farm, "create_agent")[0])["instructions"] == "Line one.\nLine two."


def test_create_reads_the_instructions_from_standard_input(run_cli, fake_farm):
    run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions-file", "-"], input="From stdin.\n")
    assert body_of(sent(fake_farm, "create_agent")[0])["instructions"] == "From stdin."


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--instructions", "x", "--instructions-file", "f.md"], "not both"),
        (["--instructions", "   "], "instructions are empty"),
        (["--from-file", "-", "--instructions-file", "-"], "only one of"),
        (["--from-file", "missing.json"], "cannot read --from-file missing.json"),
        (["--instructions-file", "missing.md"], "cannot read --instructions-file missing.md"),
    ],
)
def test_create_refuses_what_cannot_be_an_agent_with_exit_2_before_any_request(run_cli, fake_farm, args, message):
    result = run_cli(["agent", "create", "mine", "--scope", "personal", *args])
    assert result.exit_code == 2 and message in result.stderr and fake_farm.requests == []


def test_create_refuses_a_file_that_is_not_a_json_object_without_quoting_it(run_cli, fake_farm, tmp_path):
    broken, listing = tmp_path / "broken.json", tmp_path / "list.json"
    broken.write_text('{"instructions": "SECRET-LOOKING-TEXT"', encoding="utf-8")
    listing.write_text('["SECRET-LOOKING-TEXT"]', encoding="utf-8")
    first = run_cli(["agent", "create", "mine", "--from-file", str(broken)])
    second = run_cli(["agent", "create", "mine", "--from-file", str(listing)])
    assert first.exit_code == 2 and "not valid JSON" in first.stderr and "SECRET-LOOKING-TEXT" not in first.stderr
    assert second.exit_code == 2 and "must hold a JSON object (an agent)" in second.stderr
    assert "SECRET-LOOKING-TEXT" not in second.stderr and fake_farm.requests == []


def test_create_a_team_agent_without_the_admin_scope_shows_the_farms_403(run_cli, fake_farm):
    result = run_cli(["agent", "create", "shared", "--scope", "team", "--instructions", "x"])
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


def test_create_a_team_agent_as_a_tenant_admin_makes_it_visible_to_the_tenant(run_cli, fake_farm):
    assert run_cli(["agent", "create", "shared", "--scope", "team", "--instructions", "x"], token=ADMIN).exit_code == 0
    assert "shared" in {r[0] for r in rows(run_cli(["agent", "list"], token=BOB))}


def test_create_with_a_taken_slug_shows_the_conflict_code(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine")
    result = run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions", "x"])
    assert result.exit_code == 1 and "agent_slug_taken" in result.stderr


def test_create_surfaces_the_farms_422_with_its_code_and_does_not_retry(run_cli, fake_farm, sleeps):
    fake_farm.fail_next(
        "create_agent",
        status=422,
        code="agent_egress_not_allowed",
        message="egress is beyond the policy",
        retryable=False,
    )
    result = run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions", "x"])
    assert result.exit_code == 1 and "agent_egress_not_allowed" in result.stderr
    assert "egress is beyond the policy" in result.stderr and sleeps == []


def test_create_is_never_repeated_by_itself_after_a_busy_farm(run_cli, fake_farm, sleeps):
    fake_farm.fail_next("create_agent")  # 503 database_busy, retryable
    result = run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions", "x"])
    assert result.exit_code == 1 and len(sent(fake_farm, "create_agent")) == 1 and sleeps == []


def test_create_without_the_write_scope_is_a_403_not_a_crash(run_cli, fake_farm):
    result = run_cli(["agent", "create", "mine", "--scope", "personal", "--instructions", "x"], token=READONLY)
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


# -- edit ---------------------------------------------------------------------------------------------------------


def test_edit_sends_only_the_fields_that_were_given_and_leaves_the_rest_alone(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine", description="old", instructions="keep me", plugin_agent=REVIEWER)
    result = run_cli(["agent", "edit", "mine", "--description", "new"])
    assert body_of(sent(fake_farm, "update_agent")[0]) == {"description": "new"}
    assert rows(result) == [["mine", "personal", "alice", "new"]]
    kept = printed(run_cli(["agent", "view", "mine", "--json"]))
    assert (kept["instructions"], kept["plugin_agent"]) == ("keep me", REVIEWER)


def test_edit_sends_a_patch_to_the_slug_without_a_scope_when_none_was_given(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine")
    run_cli(["agent", "edit", "mine", "--description", "x"])
    request = sent(fake_farm, "update_agent")[0]
    assert (request.method, request.path, request.query) == ("PATCH", f"{API}/mine", {})


def test_edit_takes_instructions_from_a_file_and_workspace_from_a_flag(run_cli, fake_farm, tmp_path):
    make(fake_farm, ALICE, "mine", instructions="old")
    path = tmp_path / "i.md"
    path.write_text("Brand new.\n", encoding="utf-8")
    run_cli(["agent", "edit", "mine", "--instructions-file", str(path), "--no-requires-workspace"])
    assert body_of(sent(fake_farm, "update_agent")[0]) == {"instructions": "Brand new.", "requires_workspace": False}


def test_edit_clears_a_field_with_null_in_a_file(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine", instructions="old", plugin_agent=REVIEWER)
    document = json.dumps({"plugin_agent": None, "limits": None})
    assert run_cli(["agent", "edit", "mine", "--from-file", "-"], input=document).exit_code == 0
    assert body_of(sent(fake_farm, "update_agent")[0]) == {"plugin_agent": None, "limits": None}
    assert printed(run_cli(["agent", "view", "mine", "--json"]))["plugin_agent"] is None


def test_edit_flags_override_the_file(run_cli, fake_farm, tmp_path):
    make(fake_farm, ALICE, "mine")
    path = tmp_path / "e.json"
    path.write_text(json.dumps({"description": "file", "credentials": ["git-readonly"]}), encoding="utf-8")
    run_cli(["agent", "edit", "mine", "--from-file", str(path), "--description", "flag"])
    assert body_of(sent(fake_farm, "update_agent")[0]) == {"description": "flag", "credentials": ["git-readonly"]}


def test_edit_with_nothing_to_change_exits_2_without_a_request(run_cli, fake_farm, tmp_path):
    make(fake_farm, ALICE, "mine")
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    for args in (["agent", "edit", "mine"], ["agent", "edit", "mine", "--from-file", str(empty)]):
        result = run_cli(args)
        assert result.exit_code == 2 and "nothing to change" in result.stderr
    assert fake_farm.requests == []


def test_edit_scope_alone_is_not_a_change(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine")
    result = run_cli(["agent", "edit", "mine", "--scope", "personal"])
    assert result.exit_code == 2 and fake_farm.requests == []


@pytest.mark.parametrize("fixed", ["slug", "scope"])
def test_edit_refuses_a_file_that_tries_to_change_the_slug_or_the_scope(run_cli, fake_farm, tmp_path, fixed):
    make(fake_farm, ALICE, "mine")
    path = tmp_path / "e.json"
    path.write_text(json.dumps({fixed: "personal", "description": "x"}), encoding="utf-8")
    result = run_cli(["agent", "edit", "mine", "--from-file", str(path)])
    assert result.exit_code == 2 and f"{fixed} of an agent never changes" in result.stderr
    assert fake_farm.requests == []


def test_edit_scope_picks_which_of_two_agents_with_the_same_slug(run_cli, fake_farm):
    make(fake_farm, ALICE, "dup", description="personal")
    make(fake_farm, ADMIN, "dup", scope="team", description="team")
    personal = run_cli(["agent", "edit", "dup", "--description", "p2"])
    assert rows(personal)[0][1] == "personal"
    as_admin = run_cli(["agent", "edit", "dup", "--scope", "team", "--description", "t2"], token=ADMIN)
    assert rows(as_admin)[0][1:] == ["team", "", "t2"]
    assert sent(fake_farm, "update_agent")[1].query == {"scope": ["team"]}


def test_edit_a_team_agent_without_the_admin_scope_shows_the_farms_403(run_cli, fake_farm):
    make(fake_farm, ADMIN, "shared", scope="team")
    result = run_cli(["agent", "edit", "shared", "--description", "x"])
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


def test_edit_a_team_agent_with_scope_team_as_a_non_admin_is_the_farms_403(run_cli, fake_farm):
    make(fake_farm, ADMIN, "shared", scope="team")
    result = run_cli(["agent", "edit", "shared", "--scope", "team", "--description", "x"])
    assert result.exit_code == 1 and "insufficient_scope" in result.stderr


def test_edit_a_platform_agent_is_refused_by_the_farm_as_read_only(run_cli, fake_farm):
    result = run_cli(["agent", "edit", "repo-helper", "--description", "x"], token=ADMIN)
    assert result.exit_code == 1 and "agent_read_only" in result.stderr


def test_edit_somebody_elses_personal_agent_is_a_404(run_cli, fake_farm):
    make(fake_farm, BOB, "bobs")
    result = run_cli(["agent", "edit", "bobs", "--description", "x"])
    assert result.exit_code == 1 and "agent_not_found" in result.stderr


def test_edit_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine")
    answer = printed(run_cli(["agent", "edit", "mine", "--description", "d", "--json"]))
    assert answer == printed(run_cli(["agent", "view", "mine", "--json"])) and answer["description"] == "d"


def test_edit_is_never_repeated_by_itself_after_a_busy_farm(run_cli, fake_farm, sleeps):
    make(fake_farm, ALICE, "mine")
    fake_farm.fail_next("update_agent")
    assert run_cli(["agent", "edit", "mine", "--description", "x"]).exit_code == 1
    assert len(sent(fake_farm, "update_agent")) == 1 and sleeps == []


# -- delete -------------------------------------------------------------------------------------------------------


def test_delete_with_yes_removes_the_agent_and_says_so(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine")
    result = run_cli(["agent", "delete", "mine", "--yes"])
    assert result.exit_code == 0 and result.stdout == "deleted agent mine\n"
    assert [(r.method, r.path, r.status) for r in sent(fake_farm, "delete_agent")] == [("DELETE", f"{API}/mine", 204)]
    assert run_cli(["agent", "view", "mine"]).exit_code == 1


def test_delete_without_a_terminal_and_without_yes_refuses_and_changes_nothing(run_cli, fake_farm, terminal):
    terminal(False)
    make(fake_farm, ALICE, "mine")
    result = run_cli(["agent", "delete", "mine"])
    assert result.exit_code == 2 and "pass --yes" in result.stderr and fake_farm.requests == []


def test_delete_asks_on_a_terminal_and_a_no_changes_nothing(run_cli, fake_farm, terminal):
    terminal(True)
    make(fake_farm, ALICE, "mine")
    declined = run_cli(["agent", "delete", "mine"], input="n\n")
    assert declined.exit_code == 1 and "nothing was changed" in declined.stderr
    assert "Delete agent mine?" in declined.stderr and fake_farm.requests == []
    accepted = run_cli(["agent", "delete", "mine"], input="y\n")
    assert accepted.exit_code == 0 and [r.status for r in sent(fake_farm, "delete_agent")] == [204]


def test_delete_scope_picks_the_agent_and_is_named_in_the_question(run_cli, fake_farm, terminal):
    terminal(True)
    make(fake_farm, ALICE, "dup")
    make(fake_farm, ADMIN, "dup", scope="team")
    result = run_cli(["agent", "delete", "dup", "--scope", "team"], input="y\n", token=ADMIN)
    assert result.exit_code == 0 and "Delete team agent dup?" in result.stderr
    assert sent(fake_farm, "delete_agent")[0].query == {"scope": ["team"]}
    assert "dup" in {r[0] for r in rows(run_cli(["agent", "list"]))}  # alice's personal one is still there


def test_delete_an_agent_a_live_schedule_launches_shows_the_conflict(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine")
    run_cli(
        ["api", "schedules", "--input", "-"],
        input=json.dumps(
            {"task": {"agent_id": "mine"}, "recurrence": {"cron": "0 8 * * 1", "timezone": "Europe/Madrid"}}
        ),
    )
    result = run_cli(["agent", "delete", "mine", "--yes"])
    assert result.exit_code == 1 and "agent_in_use_by_schedule" in result.stderr


def test_delete_a_platform_agent_is_refused_by_the_farm_as_read_only(run_cli, fake_farm):
    result = run_cli(["agent", "delete", "repo-helper", "--yes"])
    assert result.exit_code == 1 and "agent_read_only" in result.stderr


def test_delete_somebody_elses_personal_agent_is_a_404_and_leaves_it_there(run_cli, fake_farm):
    make(fake_farm, BOB, "bobs")
    result = run_cli(["agent", "delete", "bobs", "--yes"])
    assert result.exit_code == 1 and "agent_not_found" in result.stderr
    assert "bobs" in {r[0] for r in rows(run_cli(["agent", "list"], token=BOB))}


def test_delete_is_never_repeated_by_itself_after_a_busy_farm(run_cli, fake_farm, sleeps):
    make(fake_farm, ALICE, "mine")
    fake_farm.fail_next("delete_agent")
    assert run_cli(["agent", "delete", "mine", "--yes"]).exit_code == 1
    assert len(sent(fake_farm, "delete_agent")) == 1 and sleeps == []


# -- the token, the registry and the help -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["agent", "list"],
        ["agent", "view", "repo-helper"],
        ["agent", "create", "mine", "--scope", "personal", "--instructions", "x"],
        ["agent", "edit", "mine", "--description", "x"],
        ["agent", "delete", "mine", "--yes"],
    ],
)
def test_every_agent_command_without_a_token_exits_4_before_any_request(run_cli, fake_farm, args):
    result = run_cli(args, token=None)
    assert result.exit_code == 4 and fake_farm.requests == []


def test_no_agent_command_ever_prints_the_token(run_cli, fake_farm):
    make(fake_farm, ALICE, "mine")
    for args in (
        ["agent", "list"],
        ["agent", "view", "mine"],
        ["--debug", "agent", "view", "mine"],
        ["--debug", "agent", "edit", "mine", "--description", "x"],
        ["agent", "create", "mine", "--scope", "personal", "--instructions", "x"],
        ["--debug", "agent", "delete", "mine", "--yes"],
    ):
        assert ALICE not in run_cli(args).output, args


def test_the_agent_commands_declare_the_operations_they_call():
    registry.load_all_commands()
    declared = registry.registered()
    for operation_id, command in (
        ("list_agents", "agent.list_"),
        ("get_agent", "agent.view"),
        ("create_agent", "agent.create"),
        ("update_agent", "agent.edit"),
        ("delete_agent", "agent.delete"),
    ):
        assert any(name.endswith(command) for name in declared[operation_id]), (operation_id, command)


def test_the_root_help_and_agent_help_list_the_commands(run_cli):
    assert "agent" in run_cli(["--help"]).stdout
    listed = run_cli(["agent", "--help"]).stdout
    for name in ("list", "view", "create", "edit", "delete"):
        assert f"  {name} " in listed


def test_create_and_edit_offer_the_same_flags_to_describe_an_agent(run_cli):
    for command in ("create", "edit"):
        text = run_cli(["agent", command, "--help"]).stdout
        for flag in ("--from-file", "--description", "--instructions", "--instructions-file", "--plugin-agent"):
            assert flag in text, (command, flag)
        assert "--requires-workspace" in text and "--no-requires-workspace" in text
