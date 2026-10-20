"""`dynfarm credential`: hand a secret to the farm and manage what it keeps about it.

Why these tests exist: `credential set` is the one command whose input must not be seen by anybody, the person
included, after it is typed. The farm's channel is write-only (`farm-api.md` §8), so the only chance to get the value
right is on the way in: it has to come from exactly one of the four sources (hidden prompt, standard input,
`--value-file`, `--value-env`), arrive at the farm byte for byte (one final line break removed, nothing else), and an
empty value, a mistyped confirmation or an unknown type must stop before anything is sent. There is no option that
takes the value on the command line, and the usage error for one must not repeat it. Errors from the farm
(`credential_value_invalid`, `insufficient_scope`, a 404 for someone else's) are shown with their code, never with the
value. The subprocess tests over a real socket are in `test_secret_leaks.py`; these run in-process against the fake
farm, which validates every request against the pinned OpenAPI.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fakefarm import ADMIN, ALICE, BOB, OTHER, READONLY, ApiProblem

import dynfarm.commands
from dynfarm import registry
from dynfarm.commands.credential import CREDENTIAL_TYPES

API = "/farm/api/v1/credentials"
#: The value every test hands over: distinctive enough that finding it in any output is a leak, not a collision.
SECRET = "glpat-sentinel-7f3a9c1e"


def sent(fake_farm, operation_id):
    return [r for r in fake_farm.requests if r.operation_id == operation_id]


def body_of(request) -> dict:
    return json.loads(request.body)


def rows(result) -> list[list[str]]:
    """The tab-separated rows a listing prints when stdout is a pipe."""
    assert result.exit_code == 0, result.output
    return [line.split("\t") for line in result.stdout.splitlines()]


def stored(fake_farm, name, *, subject="alice", scope="personal", tenant="tenant-a") -> str | None:
    """The value the fake farm keeps for a credential (what a task would receive), or None."""
    return fake_farm.credential_values.get((tenant, scope, subject if scope == "personal" else None, name))


def put(fake_farm, token=ALICE, *, name="git-readonly", value="arranged-value", **fields) -> None:
    """Arrange: write a credential through the API as `token`, without leaving the call in `fake_farm.requests`."""
    body = {"name": name, "type": "git_token", "value": value, **fields}
    with httpx.Client(transport=fake_farm.transport(), headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.post(fake_farm.base_url + API, json=body)
    assert response.status_code in (200, 201), response.text
    fake_farm.requests.clear()


@pytest.fixture
def terminal(monkeypatch):
    """`terminal(True)`: a person is at a terminal (prompts and confirmations), `terminal(False)`: a script."""

    def choose(tty):
        monkeypatch.setattr(dynfarm.commands, "is_interactive", lambda: tty)

    return choose


def set_args(*extra: str, name: str = "git-readonly", type_: str = "git_token") -> list[str]:
    return ["credential", "set", name, "--type", type_, *extra]


def assert_not_shown(result, secret=SECRET):
    assert secret not in result.stdout
    assert secret not in result.stderr


# -- the contract -------------------------------------------------------------------------------------------------


def test_the_types_set_accepts_are_exactly_the_enum_of_the_pinned_openapi():
    from fakefarm.farm import FIXTURE

    schema = json.loads(FIXTURE.read_text(encoding="utf-8"))["components"]["schemas"]["CredentialCreate"]
    assert list(CREDENTIAL_TYPES) == schema["properties"]["type"]["enum"]


def test_the_credential_commands_declare_the_operations_they_call():
    registry.load_all_commands()
    calls = registry.registered()
    for operation_id, callback in (
        ("list_credentials", "list_"),
        ("get_credential", "view"),
        ("write_credential", "set_"),
        ("delete_credential", "delete"),
    ):
        assert f"dynfarm.commands.credential.{callback}" in calls[operation_id]


# -- set: where the value comes from ------------------------------------------------------------------------------


def test_set_reads_the_value_from_standard_input_and_says_saved_with_the_fingerprint(run_cli, fake_farm, terminal):
    terminal(False)
    result = run_cli(set_args(), input=SECRET + "\n")
    assert result.exit_code == 0, result.output
    [request] = sent(fake_farm, "write_credential")
    assert body_of(request) == {"name": "git-readonly", "type": "git_token", "value": SECRET}
    assert stored(fake_farm, "git-readonly") == SECRET
    fingerprint = fake_farm._credentials[("tenant-a", "personal", "alice", "git-readonly")]["fingerprint"]
    assert result.stdout == f"Saved credential git-readonly (git_token, fingerprint {fingerprint})\n"
    assert_not_shown(result)


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (SECRET + "\n", SECRET),
        (SECRET + "\r\n", SECRET),
        (SECRET + "\n\n", SECRET + "\n"),
        ("line one\r\nline two\n", "line one\r\nline two"),
        (SECRET, SECRET),
        ("  padded  \n", "  padded  "),
    ],
)
def test_set_removes_one_final_line_break_from_standard_input_and_nothing_else(
    run_cli, fake_farm, terminal, given, kept
):
    terminal(False)
    result = run_cli(set_args(), input=given)
    assert result.exit_code == 0, result.output
    assert stored(fake_farm, "git-readonly") == kept


def test_set_reads_the_value_from_a_file_byte_for_byte_but_the_final_line_break(run_cli, fake_farm, tmp_path):
    secret_file = tmp_path / "token.txt"
    secret_file.write_bytes(b"multi\r\nline-" + SECRET.encode() + b"\n")
    result = run_cli(set_args("--value-file", str(secret_file)))
    assert result.exit_code == 0, result.output
    assert stored(fake_farm, "git-readonly") == "multi\r\nline-" + SECRET
    assert_not_shown(result)


def test_set_value_file_dash_reads_standard_input(run_cli, fake_farm, terminal):
    terminal(False)
    result = run_cli(set_args("--value-file", "-"), input=SECRET + "\n")
    assert result.exit_code == 0, result.output
    assert stored(fake_farm, "git-readonly") == SECRET


def test_set_reads_the_value_from_an_environment_variable_as_it_is(run_cli, fake_farm):
    result = run_cli(set_args("--value-env", "MY_GITLAB_TOKEN"), env={"MY_GITLAB_TOKEN": SECRET + "\n"})
    assert result.exit_code == 0, result.output
    assert stored(fake_farm, "git-readonly") == SECRET + "\n"
    assert_not_shown(result)


def test_set_with_an_unset_environment_variable_names_the_variable_and_sends_nothing(run_cli, fake_farm):
    result = run_cli(set_args("--value-env", "NOT_THERE_AT_ALL"))
    assert result.exit_code == 2
    assert "the environment variable NOT_THERE_AT_ALL is not set" in result.stderr
    assert fake_farm.requests == []


def test_set_with_an_unreadable_file_names_the_file_and_sends_nothing(run_cli, fake_farm, tmp_path):
    result = run_cli(set_args("--value-file", str(tmp_path / "missing.txt")))
    assert result.exit_code == 2 and "cannot read --value-file" in result.stderr
    assert fake_farm.requests == []


def test_set_refuses_a_file_that_is_not_utf8_without_quoting_it(run_cli, fake_farm, tmp_path):
    secret_file = tmp_path / "binary.key"
    secret_file.write_bytes(SECRET.encode() + b"\xff\xfe")
    result = run_cli(set_args("--value-file", str(secret_file)))
    assert result.exit_code == 2 and "is not UTF-8 text" in result.stderr
    assert_not_shown(result)
    assert fake_farm.requests == []


def test_set_refuses_two_sources_at_once(run_cli, fake_farm, tmp_path):
    result = run_cli(set_args("--value-file", str(tmp_path / "f"), "--value-env", "X"))
    assert result.exit_code == 2 and "not both" in result.stderr
    assert fake_farm.requests == []


# -- set: the hidden prompt ---------------------------------------------------------------------------------------


def test_set_on_a_terminal_asks_twice_with_a_hidden_prompt_and_never_shows_the_value(run_cli, fake_farm, terminal):
    terminal(True)
    result = run_cli(set_args(), input=f"{SECRET}\n{SECRET}\n")
    assert result.exit_code == 0, result.output
    assert "Secret value (hidden):" in result.stderr and "Repeat it:" in result.stderr
    assert stored(fake_farm, "git-readonly") == SECRET
    assert_not_shown(result)


def test_set_on_a_terminal_refuses_when_the_two_values_differ_and_sends_nothing(run_cli, fake_farm, terminal):
    terminal(True)
    result = run_cli(set_args(), input=f"{SECRET}\n{SECRET}x\n")
    assert result.exit_code == 2 and "the two values do not match; nothing was saved" in result.stderr
    assert fake_farm.requests == []
    assert_not_shown(result)


def test_set_value_file_dash_on_a_terminal_asks_with_the_hidden_prompt_instead(run_cli, fake_farm, terminal):
    terminal(True)
    result = run_cli(set_args("--value-file", "-"), input=f"{SECRET}\n{SECRET}\n")
    assert result.exit_code == 0, result.output
    assert "Secret value (hidden):" in result.stderr
    assert stored(fake_farm, "git-readonly") == SECRET


# -- set: what is refused before anything is sent ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tty", "extra", "given", "env"),
    [
        (False, [], "", None),
        (False, [], "\n", None),
        (False, [], "   \t\n", None),
        (False, ["--value-env", "EMPTY_ONE"], None, {"EMPTY_ONE": ""}),
        (True, [], "\n", None),
        (True, [], "  \n", None),
    ],
    ids=["stdin-nothing", "stdin-newline", "stdin-blank", "env-empty", "prompt-empty", "prompt-blank"],
)
def test_set_refuses_an_empty_value_with_exit_2_and_sends_nothing(run_cli, fake_farm, terminal, tty, extra, given, env):
    terminal(tty)
    result = run_cli(set_args(*extra), input=given, env=env)
    assert result.exit_code == 2 and "is empty; nothing was saved" in result.stderr
    assert fake_farm.requests == []


def test_set_refuses_an_empty_file(run_cli, fake_farm, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"\n")
    result = run_cli(set_args("--value-file", str(empty)))
    assert result.exit_code == 2 and f"--value-file {empty} is empty" in result.stderr
    assert fake_farm.requests == []


def test_set_refuses_a_type_the_farm_does_not_know_before_reading_or_sending(run_cli, fake_farm, terminal):
    terminal(False)
    result = run_cli(set_args(type_="password"), input=SECRET + "\n")
    assert result.exit_code == 2
    assert "'password' is not one of" in result.stderr and "git_token" in result.stderr
    assert fake_farm.requests == []
    assert_not_shown(result)


@pytest.mark.parametrize(
    "argv",
    [
        ["--value", SECRET],
        [f"--value={SECRET}"],
        [f"--secret={SECRET}"],
        ["--token", SECRET],
        [SECRET],
        ["--value-env", "X", SECRET, SECRET + "-again"],
    ],
    ids=["value-space", "value-equals", "secret-equals", "token-space", "positional", "two-positionals"],
)
def test_there_is_no_way_to_give_the_value_in_argv_and_the_usage_error_does_not_repeat_it(run_cli, fake_farm, argv):
    result = run_cli(set_args(*argv))
    assert result.exit_code == 2
    assert "--value-file FILE or --value-env VARIABLE" in result.stderr
    assert_not_shown(result)
    assert fake_farm.requests == []


def test_set_help_lists_no_option_that_takes_the_value(run_cli):
    result = run_cli(["credential", "set", "--help"])
    assert result.exit_code == 0
    options = {word.rstrip(",") for word in result.stdout.split() if word.startswith("--")}
    assert {"--value-file", "--value-env", "--type"} <= options
    assert not options & {"--value", "--secret", "--body", "--token", "--password"}


def test_set_metadata_needs_key_equals_value(run_cli, fake_farm):
    result = run_cli(set_args("--metadata", "no-equals-sign", "--value-env", "X"), env={"X": SECRET})
    assert result.exit_code == 2 and "--metadata takes KEY=VALUE" in result.stderr
    assert fake_farm.requests == []


def test_set_without_a_token_stops_with_exit_4_before_asking_for_the_value(run_cli, fake_farm, terminal):
    terminal(True)
    result = run_cli(set_args(), input=f"{SECRET}\n{SECRET}\n", token=None)
    assert result.exit_code == 4 and "no token for" in result.stderr
    assert "Secret value" not in result.stderr
    assert fake_farm.requests == []


# -- set: what is sent and what is said ---------------------------------------------------------------------------


def test_set_sends_scope_description_metadata_and_expiry_only_as_given(run_cli, fake_farm):
    result = run_cli(
        set_args(
            "--scope",
            "personal",
            "--description",
            "Read-only token for the manager repo",
            "--metadata",
            "repo=https://gitlab.example.com/a/b.git",
            "--metadata",
            "owner=web team=yes",
            "--expires-at",
            "2027-01-01T00:00:00Z",
            "--value-env",
            "TOKEN_VAR",
        ),
        env={"TOKEN_VAR": SECRET},
    )
    assert result.exit_code == 0, result.output
    assert body_of(sent(fake_farm, "write_credential")[0]) == {
        "name": "git-readonly",
        "type": "git_token",
        "scope": "personal",
        "description": "Read-only token for the manager repo",
        "metadata": {"repo": "https://gitlab.example.com/a/b.git", "owner": "web team=yes"},
        "expires_at": "2027-01-01T00:00:00Z",
        "value": SECRET,
    }


def test_set_of_a_name_you_already_have_says_rotated(run_cli, fake_farm):
    put(fake_farm, name="git-readonly", value="the-old-one")
    result = run_cli(set_args("--value-env", "V"), env={"V": SECRET})
    assert result.exit_code == 0, result.output
    assert sent(fake_farm, "write_credential")[0].status == 200
    assert result.stdout.startswith("Rotated credential git-readonly (git_token, fingerprint sha256:")
    assert stored(fake_farm, "git-readonly") == SECRET


def test_set_json_prints_the_metadata_and_never_the_value(run_cli, fake_farm):
    result = run_cli(set_args("--value-env", "V", "--json"), env={"V": SECRET})
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["name"] == "git-readonly" and answer["fingerprint"].startswith("sha256:")
    assert "value" not in answer
    assert_not_shown(result)


def test_set_shared_is_for_a_tenant_admin_and_says_shared(run_cli, fake_farm):
    refused = run_cli(set_args("--scope", "shared", "--value-env", "V"), env={"V": SECRET})
    assert refused.exit_code == 1 and "(insufficient_scope)" in refused.stderr
    assert_not_shown(refused)
    saved = run_cli(set_args("--scope", "shared", "--value-env", "V"), env={"V": SECRET}, token=ADMIN)
    assert saved.exit_code == 0, saved.output
    assert saved.stdout.startswith("Saved shared credential git-readonly (git_token, fingerprint sha256:")
    assert stored(fake_farm, "git-readonly", scope="shared") == SECRET


def test_set_with_a_read_only_key_is_insufficient_scope(run_cli, fake_farm):
    result = run_cli(set_args("--value-env", "V"), env={"V": SECRET}, token=READONLY)
    assert result.exit_code == 1 and "(insufficient_scope)" in result.stderr
    assert_not_shown(result)


def test_set_shows_credential_value_invalid_with_its_code_and_without_the_value(run_cli, fake_farm):
    not_https = "http://" + SECRET + ".example.com/mcp"
    result = run_cli(set_args("--value-env", "V", type_="mcp_url"), env={"V": not_https})
    assert result.exit_code == 1
    assert "(credential_value_invalid)" in result.stderr
    assert_not_shown(result, not_https)
    assert_not_shown(result)


def test_set_masks_the_value_should_the_farm_ever_quote_it_in_an_error(run_cli, fake_farm):
    def echoing(call):
        value = call.body["value"]
        raise ApiProblem(422, "credential_value_invalid", f"bad value {value}", details={"field": "value", "v": value})

    fake_farm.override("write_credential", echoing)
    result = run_cli(set_args("--value-env", "V"), env={"V": SECRET})
    assert result.exit_code == 1
    assert "bad value [redacted] (credential_value_invalid)" in result.stderr
    assert_not_shown(result)


def test_set_is_never_repeated_by_itself_after_a_retryable_error(run_cli, fake_farm, sleeps):
    fake_farm.fail_next("write_credential", status=503, code="secret_store_unavailable", retry_after=2)
    result = run_cli(set_args("--value-env", "V"), env={"V": SECRET})
    assert result.exit_code == 1 and "(secret_store_unavailable)" in result.stderr
    assert "retryable: try again in 2 s" in result.stderr
    assert len(sent(fake_farm, "write_credential")) == 1 and sleeps == []
    assert_not_shown(result)


def test_set_under_debug_traces_the_post_without_its_body_or_any_header(run_cli, fake_farm):
    result = run_cli(["--debug", *set_args("--value-env", "V")], env={"V": SECRET})
    assert result.exit_code == 0, result.output
    assert "[debug] POST /farm/api/v1/credentials (write_credential) -> 201 in " in result.stderr
    for forbidden in (SECRET, ALICE, "Bearer", "authorization", '"value"', 'git_token"'):
        assert forbidden not in result.stderr
    assert_not_shown(result)


def test_set_escapes_a_name_and_the_farm_refuses_one_that_leaves_your_space(run_cli, fake_farm):
    result = run_cli(set_args("--value-env", "V", name="../bob/git"), env={"V": SECRET})
    assert result.exit_code == 1 and "(credentials_ref_out_of_tenant)" in result.stderr
    assert_not_shown(result)


# -- list and view ------------------------------------------------------------------------------------------------


def test_list_shows_name_type_scope_and_fingerprint_of_your_own_and_never_a_value(run_cli, fake_farm):
    put(fake_farm, name="git-readonly", value=SECRET, expires_at="2027-01-01T00:00:00Z")
    put(fake_farm, name="s3-backups", value=SECRET + "-2")
    put(fake_farm, BOB, name="bobs-own", value="bob-value")
    result = run_cli(["credential", "list"])
    listed = rows(result)
    assert [row[:3] for row in listed] == [
        ["git-readonly", "git_token", "personal"],
        ["s3-backups", "git_token", "personal"],
    ]
    assert listed[0][3].startswith("sha256:") and listed[0][4] == "2027-01-01T00:00:00Z"
    assert_not_shown(result)
    assert sent(fake_farm, "list_credentials")[0].query == {}


def test_list_scope_shared_lists_the_team_s_credentials(run_cli, fake_farm):
    put(fake_farm, ADMIN, name="web-api", value=SECRET, scope="shared")
    put(fake_farm, name="mine", value="x-value")
    result = run_cli(["credential", "list", "--scope", "shared"])
    assert [row[:3] for row in rows(result)] == [["web-api", "git_token", "shared"]]
    assert sent(fake_farm, "list_credentials")[0].query == {"scope": ["shared"]}


def test_list_json_is_the_farm_s_answer(run_cli, fake_farm):
    put(fake_farm, name="git-readonly", value=SECRET)
    result = run_cli(["credential", "list", "--json"])
    assert result.exit_code == 0
    [item] = json.loads(result.stdout)["items"]
    assert item["name"] == "git-readonly" and "value" not in item
    assert_not_shown(result)


def test_view_shows_the_metadata_of_a_credential(run_cli, fake_farm):
    put(fake_farm, name="git-readonly", value=SECRET, description="For the manager repo", metadata={"repo": "r"})
    result = run_cli(["credential", "view", "git-readonly"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["name:", "git-readonly"]
    assert "description: For the manager repo" in result.stdout and "metadata:    repo=r" in result.stdout
    assert "fingerprint: sha256:" in result.stdout
    assert_not_shown(result)


def test_view_json_and_scope_shared(run_cli, fake_farm):
    put(fake_farm, ADMIN, name="web-api", value=SECRET, scope="shared")
    result = run_cli(["credential", "view", "web-api", "--scope", "shared", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["scope"] == "shared"
    assert sent(fake_farm, "get_credential")[0].query == {"scope": ["shared"]}


@pytest.mark.parametrize("token", [BOB, OTHER], ids=["same-tenant-other-person", "other-tenant"])
def test_view_of_someone_else_s_credential_is_not_found(run_cli, fake_farm, token):
    put(fake_farm, name="git-readonly", value=SECRET)
    result = run_cli(["credential", "view", "git-readonly"], token=token)
    assert result.exit_code == 1 and "(credentials_not_found)" in result.stderr


def test_view_escapes_the_name_into_one_path_segment(run_cli, fake_farm):
    result = run_cli(["credential", "view", "a/b"])
    assert result.exit_code == 1
    assert sent(fake_farm, "get_credential")[0].target.startswith(f"{API}/a%2Fb")


# -- delete -------------------------------------------------------------------------------------------------------


def test_delete_with_yes_deletes_and_says_running_tasks_keep_their_copy(run_cli, fake_farm):
    put(fake_farm, name="git-readonly", value=SECRET)
    result = run_cli(["credential", "delete", "git-readonly", "--yes"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "deleted credential git-readonly\n"
    assert "running tasks keep their copy" in result.stderr
    assert stored(fake_farm, "git-readonly") is None
    assert [(r.method, r.status) for r in sent(fake_farm, "delete_credential")] == [("DELETE", 204)]


def test_delete_without_a_terminal_and_without_yes_refuses_and_changes_nothing(run_cli, fake_farm, terminal):
    terminal(False)
    put(fake_farm, name="git-readonly", value=SECRET)
    result = run_cli(["credential", "delete", "git-readonly"])
    assert result.exit_code == 2 and "pass --yes" in result.stderr
    assert sent(fake_farm, "delete_credential") == [] and stored(fake_farm, "git-readonly") == SECRET


def test_delete_asks_on_a_terminal_and_a_no_changes_nothing(run_cli, fake_farm, terminal):
    terminal(True)
    put(fake_farm, name="git-readonly", value=SECRET)
    declined = run_cli(["credential", "delete", "git-readonly"], input="n\n")
    assert declined.exit_code == 1 and "nothing was changed" in declined.stderr
    assert "Delete credential git-readonly?" in declined.stderr and stored(fake_farm, "git-readonly") == SECRET
    accepted = run_cli(["credential", "delete", "git-readonly"], input="y\n")
    assert accepted.exit_code == 0 and stored(fake_farm, "git-readonly") is None


def test_delete_shared_needs_a_tenant_admin_and_warns_about_the_team_s_agents(run_cli, fake_farm):
    put(fake_farm, ADMIN, name="web-api", value=SECRET, scope="shared")
    refused = run_cli(["credential", "delete", "web-api", "--scope", "shared", "--yes"])
    assert refused.exit_code == 1 and "(insufficient_scope)" in refused.stderr
    done = run_cli(["credential", "delete", "web-api", "--scope", "shared", "--yes"], token=ADMIN)
    assert done.exit_code == 0 and done.stdout == "deleted shared credential web-api\n"
    assert "the team's agents that declare it cannot be launched" in done.stderr


def test_delete_of_someone_else_s_credential_is_not_found_and_keeps_it(run_cli, fake_farm):
    put(fake_farm, BOB, name="git-readonly", value="bob-value")
    result = run_cli(["credential", "delete", "git-readonly", "--yes"])
    assert result.exit_code == 1 and "(credentials_not_found)" in result.stderr
    assert stored(fake_farm, "git-readonly", subject="bob") == "bob-value"
