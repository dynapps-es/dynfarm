"""`dynfarm auth` as a person and a script meet it: log in, check, print, log out, switch farms.

Why these tests exist: the auth commands are where a token enters the tool, so they hold its most important promises.
A token the farm refuses is never saved; an expired token is told apart from an invalid one (`farm-api.md` §3.1); the
farm is never guessed (no default host); plain `http` to a remote farm is refused before the token is read or sent;
and no command but `auth token` ever shows the token, which several tests check by searching every byte of stdout and
stderr for a sentinel token. The fake farm answers `/whoami`; the keyring is the in-memory one of `conftest.py`.
"""

from __future__ import annotations

import getpass
import json
import os
import stat
import tomllib

import httpx
import pytest
from click.testing import CliRunner
from fakefarm import ALICE, BOB, EXPIRED, HOST
from keyring.backends import fail

import dynfarm.commands
from dynfarm import _brand, registry
from dynfarm.cli import cli
from dynfarm.config import config_path
from dynfarm.context import AppContext
from dynfarm.tokens import TokenStore

SERVICE = _brand.KEYRING_SERVICE
OTHER_HOST = "https://other.test"
LEGACY = _brand.LEGACY_DEFAULT_HOST
#: A token only these tests use, distinctive enough that finding it anywhere in the output is a leak, not a collision.
SENTINEL = "tok-sentinel-a17c9e5d"


@pytest.fixture
def farm(fake_farm):
    """The fake farm, which also accepts `SENTINEL` as alice's token."""
    fake_farm.tokens[SENTINEL] = fake_farm.principal(ALICE)
    return fake_farm


@pytest.fixture
def invoke(farm, sleeps, monkeypatch):
    """`invoke(["auth", ...], input=..., tty=False, transport=None)`: the real `dynfarm`, against the fake farm, with
    the real token store (no injected token), and a terminal or not as `tty` says."""

    def run(args, input=None, *, tty=False, transport=None):
        monkeypatch.setattr(dynfarm.commands, "is_interactive", lambda: tty)
        app = AppContext(transport=transport or farm.transport(), sleep=sleeps.append)
        return CliRunner().invoke(cli, args, input=input, obj=app, prog_name=_brand.NAME)

    return run


def login(invoke, token=SENTINEL, host=HOST, *extra, **options):
    return invoke(["auth", "login", "--hostname", host, "--with-token", *extra], input=f"{token}\n", **options)


def read_config() -> dict:
    return tomllib.loads(config_path().read_text())


def assert_never_shown(result, token=SENTINEL):
    assert token not in result.stdout
    assert token not in result.stderr


# -- login --------------------------------------------------------------------------------------------------------


def test_login_checks_the_token_then_keeps_it_in_the_keyring_and_makes_the_farm_the_default(
    invoke, farm, memory_keyring
):
    result = login(invoke)
    assert result.exit_code == 0, result.output
    assert f"Logged in to {HOST} as alice (dynapps-internal)" in result.stderr
    assert "Token stored in the system keyring" in result.stderr
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL
    assert read_config() == {"default_host": HOST, "hosts": {HOST: {}}}
    assert [r.operation_id for r in farm.requests] == ["whoami"]
    assert result.stdout == ""
    assert_never_shown(result)


@pytest.mark.parametrize(
    ("token", "message"),
    [("tok-nobody", "the farm rejected your token (token_invalid)"), (EXPIRED, "your token expired")],
)
def test_login_does_not_save_a_token_the_farm_rejects_and_exits_4(invoke, memory_keyring, token, message):
    result = login(invoke, token)
    assert result.exit_code == 4
    assert message in result.stderr
    assert "the token was not saved" in result.stderr
    assert memory_keyring.entries == {}
    assert not config_path().exists()
    assert_never_shown(result, token)


def test_login_does_not_save_a_token_it_could_not_check(invoke, memory_keyring):
    def unreachable(request):
        raise httpx.ConnectError("connection refused")

    result = login(invoke, transport=httpx.MockTransport(unreachable))
    assert result.exit_code == 1
    assert "could not reach https://farm.test" in result.stderr
    assert memory_keyring.entries == {}
    assert_never_shown(result)


def test_login_reads_one_line_from_stdin_and_strips_it(invoke, memory_keyring):
    result = invoke(["auth", "login", "--hostname", HOST, "--with-token"], input=f"  {SENTINEL}  \nsecond line\n")
    assert result.exit_code == 0, result.output
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL


def test_login_with_an_empty_stdin_is_a_usage_error(invoke, farm):
    result = invoke(["auth", "login", "--hostname", HOST, "--with-token"], input="\n")
    assert result.exit_code == 2
    assert "standard input had no token" in result.stderr
    assert farm.requests == []


def test_login_on_a_terminal_asks_for_the_token_with_a_hidden_prompt(invoke, memory_keyring):
    result = invoke(["auth", "login", "--hostname", HOST], input=f"{SENTINEL}\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "Paste your token" in result.stderr
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL
    assert_never_shown(result)


def test_login_without_a_terminal_and_without_with_token_refuses_to_prompt(invoke, farm):
    result = invoke(["auth", "login", "--hostname", HOST], input=f"{SENTINEL}\n")
    assert result.exit_code == 2
    assert "--with-token" in result.stderr
    assert farm.requests == []


def test_login_asks_for_the_farm_on_a_terminal_when_none_is_given(invoke, memory_keyring):
    result = invoke(["auth", "login"], input=f"farm.test\n{SENTINEL}\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "Farm URL" in result.stderr
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL


def test_login_never_guesses_a_farm_without_a_terminal(invoke, farm):
    result = invoke(["auth", "login", "--with-token"], input=f"{SENTINEL}\n")
    assert result.exit_code == 2
    assert "pass --hostname" in result.stderr
    assert farm.requests == []


def test_login_takes_the_farm_from_dynfarm_host(invoke, memory_keyring, monkeypatch):
    monkeypatch.setenv("DYNFARM_HOST", "farm.test")
    result = invoke(["auth", "login", "--with-token"], input=f"{SENTINEL}\n")
    assert result.exit_code == 0, result.output
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL


def test_login_refuses_plain_http_to_a_remote_farm_before_reading_or_sending_the_token(invoke, farm, memory_keyring):
    result = login(invoke, host="http://farm.example.com")
    assert result.exit_code == 2
    assert "refusing to send a token over plain http" in result.stderr
    assert farm.requests == []
    assert memory_keyring.entries == {}
    assert not config_path().exists()


def test_login_offers_the_farm_py_key_on_a_terminal_for_farm_py_s_farm(invoke, memory_keyring):
    memory_keyring.entries[(_brand.LEGACY_KEYRING_SERVICE, getpass.getuser())] = SENTINEL
    result = invoke(["auth", "login", "--hostname", LEGACY], input="y\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "farm.py" in result.stderr and _brand.LEGACY_KEYRING_SERVICE in result.stderr
    assert "Paste your token" not in result.stderr
    assert memory_keyring.entries[(SERVICE, LEGACY)] == SENTINEL
    assert_never_shown(result)


def test_login_does_not_move_the_farm_py_key_to_another_farm_because_farm_py_s_url_variable_is_set(
    invoke, memory_keyring, monkeypatch
):
    monkeypatch.setenv("AGENT_FARM_URL", HOST)  # farm.py's variable: not read, so it cannot widen the key's farm
    memory_keyring.entries[(_brand.LEGACY_KEYRING_SERVICE, getpass.getuser())] = BOB
    result = invoke(["auth", "login", "--hostname", HOST], input=f"{SENTINEL}\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "farm.py" not in result.stderr
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL
    assert_never_shown(result, BOB)


def test_login_without_a_terminal_does_not_take_its_farm_from_the_farm_py_url_variable(invoke, farm, monkeypatch):
    monkeypatch.setenv("AGENT_FARM_URL", HOST)
    result = invoke(["auth", "login", "--with-token"], input=f"{SENTINEL}\n")
    assert result.exit_code == 2
    assert "which farm? pass --hostname URL" in result.stderr
    assert farm.requests == []


def test_the_farm_py_key_is_not_offered_for_another_farm(invoke, memory_keyring):
    memory_keyring.entries[(_brand.LEGACY_KEYRING_SERVICE, getpass.getuser())] = BOB
    result = invoke(["auth", "login", "--hostname", HOST], input=f"{SENTINEL}\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "farm.py" not in result.stderr
    assert "Paste your token" in result.stderr
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL


def test_declining_the_farm_py_key_asks_for_another_token(invoke, memory_keyring):
    memory_keyring.entries[(_brand.LEGACY_KEYRING_SERVICE, getpass.getuser())] = SENTINEL
    result = invoke(["auth", "login", "--hostname", LEGACY], input=f"n\n{BOB}\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "as bob" in result.stderr
    assert memory_keyring.entries[(SERVICE, LEGACY)] == BOB


def test_the_farm_py_key_is_offered_only_on_a_terminal(invoke, memory_keyring):
    memory_keyring.entries[(_brand.LEGACY_KEYRING_SERVICE, getpass.getuser())] = BOB
    result = login(invoke, SENTINEL, LEGACY)
    assert result.exit_code == 0, result.output
    assert "farm.py" not in result.stderr
    assert memory_keyring.entries[(SERVICE, LEGACY)] == SENTINEL


def test_the_farm_py_key_is_not_offered_when_the_keyring_does_not_have_it(invoke):
    result = invoke(["auth", "login", "--hostname", HOST], input=f"{SENTINEL}\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "farm.py" not in result.stderr


def test_login_with_insecure_storage_keeps_the_token_in_a_0600_file(invoke, memory_keyring):
    result = login(invoke, SENTINEL, HOST, "--insecure-storage")
    assert result.exit_code == 0, result.output
    assert read_config()["hosts"][HOST]["token"] == SENTINEL
    if os.name == "posix":
        assert stat.S_IMODE(config_path().stat().st_mode) == 0o600
    assert (SERVICE, HOST) not in memory_keyring.entries
    assert "plain text" in result.stderr
    assert_never_shown(result)


def test_login_without_a_keyring_falls_back_to_the_file_with_a_warning(invoke, use_keyring):
    use_keyring(fail.Keyring())
    result = login(invoke)
    assert result.exit_code == 0, result.output
    assert "warning: no system keyring: the token is stored in plain text" in result.stderr
    assert read_config()["hosts"][HOST]["token"] == SENTINEL
    assert_never_shown(result)


def test_a_second_login_keeps_the_first_farm_as_the_default(invoke):
    login(invoke)
    result = login(invoke, BOB, OTHER_HOST)
    assert result.exit_code == 0, result.output
    assert "default farm" not in result.stderr
    assert read_config()["default_host"] == HOST


def test_login_and_logout_warn_when_dynfarm_token_takes_precedence_over_the_stored_one(invoke, monkeypatch):
    monkeypatch.setenv("DYNFARM_TOKEN", BOB)
    logged_in = login(invoke)
    assert logged_in.exit_code == 0, logged_in.output
    assert "warning: DYNFARM_TOKEN is set and takes precedence over the stored token" in logged_in.stderr
    logged_out = invoke(["auth", "logout", "--hostname", HOST, "--yes"])
    assert logged_out.exit_code == 0, logged_out.output
    assert "warning: DYNFARM_TOKEN is still set in this environment" in logged_out.stderr
    assert_never_shown(logged_in, BOB)
    assert_never_shown(logged_out, BOB)


def test_the_farm_py_token_variable_is_not_read_by_login_logout_status_or_token(invoke, farm, monkeypatch):
    monkeypatch.setenv("AGENT_FARM_TOKEN", BOB)  # farm.py's variable: only DYNFARM_* variables are read
    logged_in = login(invoke)
    assert logged_in.exit_code == 0, logged_in.output
    farm.requests.clear()
    logged_out = invoke(["auth", "logout", "--hostname", HOST, "--yes"])
    assert logged_out.exit_code == 0, logged_out.output
    assert "AGENT_FARM_TOKEN" not in logged_in.stderr + logged_out.stderr
    status = invoke(["auth", "status", "--hostname", HOST])
    assert status.exit_code == 1
    assert status.stdout.splitlines() == [HOST, f"  not logged in — run `dynfarm auth login --hostname {HOST}`"]
    as_json = invoke(["auth", "status", "--hostname", HOST, "--json"])
    assert json.loads(as_json.stdout) == [{"host": HOST, "default": False, "state": "logged_out", "source": None}]
    token = invoke(["auth", "token", "--hostname", HOST])
    assert token.exit_code == 4
    assert token.stdout == ""
    assert all("authorization" not in r.headers for r in farm.requests)
    assert farm.requests == []
    assert_never_shown(status, BOB)


def test_the_farm_py_url_variable_names_no_farm_for_status_token_or_switch(invoke, farm, monkeypatch):
    monkeypatch.setenv("AGENT_FARM_URL", HOST)
    status = invoke(["auth", "status"])
    assert status.exit_code == 4
    assert "not logged in to any farm" in status.stderr
    token = invoke(["auth", "token"])
    assert token.exit_code == 2
    assert "no farm configured" in token.stderr
    assert farm.requests == []


# -- status -------------------------------------------------------------------------------------------------------


def test_status_reports_two_farms_one_fine_and_one_expired_and_exits_1(invoke):
    login(invoke)
    TokenStore().remember(OTHER_HOST, EXPIRED, insecure=True)  # `login` would refuse it: it expired since
    result = invoke(["auth", "status"])
    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        f"{HOST} (default)",
        "  token from: keyring",
        "  logged in as alice (dynapps-internal)",
        "  scopes: farm:credentials:read, farm:credentials:write, farm:tasks:cancel, farm:tasks:read, farm:tasks:write",
        "  expires: 2027-01-01T00:00:00Z",
        OTHER_HOST,
        "  token from: file",
        f"  expired — get a new token and run `dynfarm auth login --hostname {OTHER_HOST}`",
    ]
    assert_never_shown(result)
    assert_never_shown(result, EXPIRED)


def test_status_tells_an_invalid_token_from_an_expired_one(invoke):
    TokenStore().remember(HOST, "tok-revoked")
    result = invoke(["auth", "status"])
    assert result.exit_code == 1
    assert "  invalid — the farm rejects this token" in result.stdout
    assert "expired" not in result.stdout
    assert_never_shown(result, "tok-revoked")


def test_status_exits_0_when_every_farm_accepts_its_token(invoke):
    login(invoke)
    login(invoke, BOB, OTHER_HOST)
    result = invoke(["auth", "status"])
    assert result.exit_code == 0, result.output
    assert "logged in as alice" in result.stdout and "logged in as bob" in result.stdout
    assert_never_shown(result)
    assert_never_shown(result, BOB)


def test_status_as_json_gives_source_state_and_identity_but_no_token(invoke):
    login(invoke)
    result = invoke(["auth", "status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "host": HOST,
            "default": True,
            "state": "ok",
            "source": "keyring",
            "subject": "alice",
            "tenant": "dynapps-internal",
            "scopes": [
                "farm:credentials:read",
                "farm:credentials:write",
                "farm:tasks:cancel",
                "farm:tasks:read",
                "farm:tasks:write",
            ],
            "is_admin": False,
            "expires_at": "2027-01-01T00:00:00Z",
        }
    ]
    assert_never_shown(result)


def test_status_reports_a_farm_it_cannot_reach(invoke, sleeps):
    login(invoke)

    def unreachable(request):
        raise httpx.ConnectError("connection refused")

    result = invoke(["auth", "status", "--json"], transport=httpx.MockTransport(unreachable))
    assert result.exit_code == 1
    row = json.loads(result.stdout)[0]
    assert row["state"] == "unreachable"
    assert row["error"]["code"] == "connection_failed"
    human = invoke(["auth", "status"], transport=httpx.MockTransport(unreachable))
    assert "  unreachable — could not reach https://farm.test" in human.stdout
    assert_never_shown(human)


def test_status_names_the_environment_variable_a_token_came_from(invoke, monkeypatch):
    monkeypatch.setenv("DYNFARM_HOST", HOST)
    monkeypatch.setenv("DYNFARM_TOKEN", SENTINEL)
    result = invoke(["auth", "status"])
    assert result.exit_code == 0, result.output
    assert "  token from: env:DYNFARM_TOKEN" in result.stdout
    assert_never_shown(result)


def test_status_with_no_farm_at_all_exits_4(invoke, farm):
    result = invoke(["auth", "status"])
    assert result.exit_code == 4
    assert "not logged in to any farm" in result.stderr
    assert farm.requests == []


def test_status_for_a_farm_with_no_token_says_how_to_log_in(invoke, farm):
    result = invoke(["auth", "status", "--hostname", OTHER_HOST])
    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        OTHER_HOST,
        f"  not logged in — run `dynfarm auth login --hostname {OTHER_HOST}`",
    ]
    assert farm.requests == []


# -- token --------------------------------------------------------------------------------------------------------


def test_auth_token_prints_exactly_the_token_and_nothing_else(invoke):
    login(invoke)
    result = invoke(["auth", "token"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{SENTINEL}\n"
    assert result.stderr == ""


def test_auth_token_for_another_farm_prints_that_farms_token(invoke):
    login(invoke)
    login(invoke, BOB, OTHER_HOST)
    assert invoke(["auth", "token", "--hostname", "other.test"]).stdout == f"{BOB}\n"


def test_auth_token_without_a_token_exits_4_and_prints_nothing_on_stdout(invoke, monkeypatch):
    monkeypatch.setenv("DYNFARM_HOST", HOST)
    result = invoke(["auth", "token"])
    assert result.exit_code == 4
    assert result.stdout == ""
    assert f"no token for {HOST}" in result.stderr


# -- logout -------------------------------------------------------------------------------------------------------


def test_logout_removes_the_token_from_both_stores_and_forgets_the_farm(invoke, memory_keyring):
    login(invoke, SENTINEL, HOST, "--insecure-storage")
    memory_keyring.entries[(SERVICE, HOST)] = SENTINEL  # an older keyring login of the same farm
    result = invoke(["auth", "logout", "--yes"])
    assert result.exit_code == 0, result.output
    assert f"Logged out of {HOST} (token removed from: keyring, file)" in result.stderr
    assert memory_keyring.entries == {}
    assert read_config() == {}
    assert invoke(["auth", "token", "--hostname", HOST]).exit_code == 4
    assert_never_shown(result)


def test_logout_says_the_token_is_not_revoked(invoke):
    login(invoke)
    result = invoke(["auth", "logout", "--yes"])
    assert "The token was not revoked: it works until it expires" in result.stderr


def test_logout_without_a_terminal_needs_yes_and_changes_nothing(invoke, memory_keyring):
    login(invoke)
    result = invoke(["auth", "logout"])
    assert result.exit_code == 2
    assert "pass --yes" in result.stderr
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL


def test_logout_asks_on_a_terminal_and_a_no_keeps_everything(invoke, memory_keyring):
    login(invoke)
    result = invoke(["auth", "logout"], input="n\n", tty=True)
    assert result.exit_code == 1
    assert f"Log out of {HOST}?" in result.stderr
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL
    assert invoke(["auth", "logout"], input="y\n", tty=True).exit_code == 0
    assert memory_keyring.entries == {}


def test_logging_out_of_the_default_farm_makes_the_other_one_the_default(invoke):
    login(invoke)
    login(invoke, BOB, OTHER_HOST)
    result = invoke(["auth", "logout", "--hostname", HOST, "--yes"])
    assert result.exit_code == 0, result.output
    assert f"The default farm is {OTHER_HOST}" in result.stderr
    assert read_config() == {"default_host": OTHER_HOST, "hosts": {OTHER_HOST: {}}}


def test_logout_of_a_farm_never_logged_in_to_fails(invoke):
    login(invoke)
    result = invoke(["auth", "logout", "--hostname", OTHER_HOST, "--yes"])
    assert result.exit_code == 1
    assert f"not logged in to {OTHER_HOST}" in result.stderr


# -- switch -------------------------------------------------------------------------------------------------------


def test_switch_makes_another_farm_the_default_and_commands_follow_it(invoke):
    login(invoke)
    login(invoke, BOB, OTHER_HOST)
    result = invoke(["auth", "switch", "--hostname", "other.test"])
    assert result.exit_code == 0, result.output
    assert f"{OTHER_HOST} is now the default farm" in result.stderr
    assert read_config()["default_host"] == OTHER_HOST
    assert invoke(["auth", "token"]).stdout == f"{BOB}\n"
    assert_never_shown(result, BOB)


def test_switch_to_a_farm_not_logged_in_to_is_a_usage_error(invoke):
    login(invoke)
    result = invoke(["auth", "switch", "--hostname", OTHER_HOST])
    assert result.exit_code == 2
    assert f"not logged in to {OTHER_HOST}" in result.stderr
    assert read_config()["default_host"] == HOST


def test_switch_needs_the_farm_to_switch_to(invoke):
    assert invoke(["auth", "switch"]).exit_code == 2


def test_switch_warns_when_dynfarm_host_overrides_the_default(invoke, monkeypatch):
    login(invoke)
    login(invoke, BOB, OTHER_HOST)
    monkeypatch.setenv("DYNFARM_HOST", HOST)
    result = invoke(["auth", "switch", "--hostname", OTHER_HOST])
    assert "warning: DYNFARM_HOST is set and overrides the default farm" in result.stderr


# -- never leak ---------------------------------------------------------------------------------------------------


def test_no_auth_command_but_token_ever_shows_the_token_even_with_debug(invoke, farm):
    results = [
        login(invoke),
        invoke(["--debug", "auth", "status"]),
        invoke(["--debug", "auth", "status", "--json"]),
        invoke(["auth", "login", "--hostname", OTHER_HOST], input=f"{SENTINEL}\n", tty=True),
        invoke(["auth", "switch", "--hostname", OTHER_HOST]),
        invoke(["--debug", "auth", "logout", "--yes"]),
        invoke(["auth", "logout", "--hostname", HOST, "--yes"]),
    ]
    assert [r.exit_code for r in results] == [0, 0, 0, 0, 0, 0, 0]
    assert "-> 200" in results[1].stderr  # the debug trace ran, and still did not show it
    for result in results:
        assert_never_shown(result)
    assert all(SENTINEL not in r.target for r in farm.requests)
    assert {r.headers["authorization"] for r in farm.requests} == {f"Bearer {SENTINEL}"}


def test_login_and_status_declare_the_operation_they_call():
    registry.load_all_commands()
    assert {"dynfarm.commands.auth.login", "dynfarm.commands.auth.status"} <= set(registry.registered()["whoami"])


def test_auth_lists_its_five_subcommands(invoke):
    result = invoke(["auth", "--help"])
    for name in ("login", "status", "token", "logout", "switch"):
        assert name in result.stdout
