"""`whoami`, `plugin list` and `callback-key`: the first commands a person runs, and the ones that explain a 404.

Why these tests exist: `whoami` is how a person (and a script) learns which subject, tenant and scopes a token carries,
so two tenants and two people must each see their own. `plugin list` is the catalogue read, human table or JSON.
`callback-key` prints public key material that a verifier copies verbatim, so it must come out exactly as the farm sent
it. All three declare the operation they call, which the parity test reads.
"""

from __future__ import annotations

import json

from fakefarm import ADMIN, ALICE, BOB, EXPIRED, OTHER, READONLY

from dynfarm import registry


def fields(result) -> dict[str, str]:
    assert result.exit_code == 0, result.output
    return {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in result.stdout.splitlines()}


# -- whoami -------------------------------------------------------------------------------------------------------


def test_whoami_shows_subject_tenant_scopes_and_expiry(run_cli):
    shown = fields(run_cli(["whoami"]))
    assert shown["subject"] == "alice"
    assert shown["tenant"] == "tenant-a"
    assert "farm:tasks:read" in shown["scopes"] and "farm:credentials:write" in shown["scopes"]
    assert shown["tenant admin"] == "no"
    assert shown["free prompt"] == "yes"
    assert shown["expires"] == "2027-01-01T00:00:00Z"


def test_whoami_tells_two_tenants_and_two_people_apart(run_cli):
    seen = {token: fields(run_cli(["whoami"], token=token)) for token in (ALICE, BOB, OTHER, ADMIN)}
    assert [(s["subject"], s["tenant"]) for s in seen.values()] == [
        ("alice", "tenant-a"),
        ("bob", "tenant-a"),
        ("carol", "acme"),
        ("dana", "tenant-a"),
    ]
    assert seen[ADMIN]["tenant admin"] == "yes" and seen[ALICE]["tenant admin"] == "no"
    assert "farm:tenant:admin" in seen[ADMIN]["scopes"]


def test_whoami_lists_only_the_scopes_the_token_has(run_cli):
    scopes = fields(run_cli(["whoami"], token=READONLY))["scopes"]
    assert scopes == "farm:credentials:read, farm:tasks:read"


def test_whoami_json_is_the_farms_answer_verbatim(run_cli):
    result = run_cli(["whoami", "--json"], token=OTHER)
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "subject": "carol",
        "tenant": "acme",
        "scopes": sorted(
            ["farm:credentials:read", "farm:credentials:write", "farm:tasks:cancel", "farm:tasks:read"]
            + ["farm:tasks:write"]
        ),
        "is_admin": False,
        "expires_at": "2027-01-01T00:00:00Z",
        "free_prompt": True,
    }


def test_whoami_shows_identity_fields_the_farm_adds_and_hides_the_empty_ones(run_cli, fake_farm):
    original = fake_farm._op_whoami

    def with_identity(call):
        status, body = original(call)
        return status, {**body, "display_name": "Alice \x1b[31mA.", "email": "alice@example.test", "groups": None}

    fake_farm.override("whoami", with_identity)
    shown = fields(run_cli(["whoami"]))
    fake_farm.take_violations()  # the pinned OpenAPI does not know these fields; the point is how they print
    assert shown["email"] == "alice@example.test"
    assert shown["display_name"] == "Alice  [31mA."  # the escape character is replaced, not printed
    assert "groups" not in shown
    assert "\x1b" not in "".join(shown.values())


def test_whoami_with_an_expired_token_exits_4_and_says_to_renew(run_cli):
    result = run_cli(["whoami"], token=EXPIRED)
    assert result.exit_code == 4 and result.stdout == ""
    assert "expired" in result.stderr and "dynfarm auth login" in result.stderr


def test_whoami_with_an_unknown_token_exits_4(run_cli):
    result = run_cli(["whoami"], token="tok-nobody")
    assert result.exit_code == 4 and "rejected" in result.stderr


def test_whoami_without_a_token_exits_4_before_any_request(run_cli, fake_farm):
    result = run_cli(["whoami"], token=None)
    assert result.exit_code == 4 and fake_farm.requests == []


def test_whoami_never_prints_the_token(run_cli):
    for args in (["whoami"], ["whoami", "--json"], ["--debug", "whoami"]):
        assert ALICE not in run_cli(args).output


# -- plugin list --------------------------------------------------------------------------------------------------


def test_plugin_list_prints_one_row_per_plugin_with_its_agents(run_cli):
    result = run_cli(["plugin", "list"])
    assert result.exit_code == 0, result.output
    rows = [line.split("\t") for line in result.stdout.splitlines()]
    assert rows == [
        ["example-plugin", "1.4.0", "0a1b2c3d", "example-plugin:code-reviewer"],
        ["other-plugin", "2.0.1", "", ""],
    ]


def test_plugin_list_json_is_the_farms_answer_verbatim(run_cli, fake_farm):
    result = run_cli(["plugin", "list", "--json"])
    assert json.loads(result.stdout) == {"items": fake_farm._plugins}


def test_plugin_list_draws_a_table_with_headers_on_a_terminal(run_cli, monkeypatch):
    import dynfarm.output as output

    monkeypatch.setattr(output, "_is_tty", lambda stream: True)
    monkeypatch.setenv("NO_COLOR", "1")
    lines = run_cli(["plugin", "list"]).stdout.splitlines()
    assert lines[0].split() == ["NAME", "VERSION", "COMMIT", "AGENTS"]
    assert lines[1].split() == ["example-plugin", "1.4.0", "0a1b2c3d", "example-plugin:code-reviewer"]
    assert lines[2].split() == ["other-plugin", "2.0.1", "-", "-"]


def test_plugin_list_needs_a_valid_token(run_cli):
    assert run_cli(["plugin", "list"], token="tok-nobody").exit_code == 4


def test_plugin_alone_shows_its_subcommands(run_cli):
    result = run_cli(["plugin", "--help"])
    assert result.exit_code == 0 and "list" in result.stdout


# -- callback-key -------------------------------------------------------------------------------------------------


def test_callback_key_prints_the_key_exactly_as_the_farm_sent_it(run_cli, fake_farm):
    result = run_cli(["callback-key"])
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["keys"][0]["kid"] == "fake-key-1" and answer["keys"][0]["crv"] == "Ed25519"
    assert answer["keys"][0]["pem"] == "-----BEGIN PUBLIC KEY-----\nFAKE\n-----END PUBLIC KEY-----\n"
    assert answer == fake_farm._op_callback_key(None)[1]


def test_callback_key_pem_prints_only_the_pem_ready_for_a_file(run_cli):
    result = run_cli(["callback-key", "--pem"])
    assert result.exit_code == 0
    assert result.stdout == "-----BEGIN PUBLIC KEY-----\nFAKE\n-----END PUBLIC KEY-----\n"


def test_callback_key_pem_says_so_when_the_farm_sent_none(run_cli, fake_farm):
    fake_farm.override("callback_key", lambda call: (200, {"keys": []}))
    result = run_cli(["callback-key", "--pem"])
    assert result.exit_code == 1 and "no PEM key" in result.stderr


# -- registry and help --------------------------------------------------------------------------------------------


def test_the_commands_declare_the_operations_they_call():
    registry.load_all_commands()
    declared = registry.registered()
    for operation_id, command in (
        ("whoami", "identity.whoami"),
        ("list_plugins", "identity.list_plugins"),
        ("callback_key", "identity.callback_key"),
    ):
        assert any(name.endswith(command) for name in declared[operation_id]), operation_id


def test_the_root_help_lists_the_new_commands(run_cli):
    result = run_cli(["--help"])
    for name in ("whoami", "plugin", "callback-key", "api"):
        assert name in result.stdout
