"""One client, one way to fail: how `FarmClient` and `@needs_client` talk to the farm and report what went wrong.

Every command calls the farm through this code, so its guarantees are pinned here. Tokens: only in
the `Authorization` header, never over plain http to a remote host, never in the debug trace or a message. Errors:
branched on the farm's `error.code` (`farm-api.md` §15), with `token_expired` told apart from `token_invalid` (§3.1)
and mapped to the exit codes a script relies on. Retries: only a `GET`, only when the farm says it is retryable, as
long as the farm's `Retry-After` says (capped), because repeating a `POST` could launch twice.
"""

from __future__ import annotations

import click
import httpx
import pytest
from click.testing import CliRunner
from fakefarm import ADMIN, ALICE, EXPIRED, HOST

import dynfarm.cli as cli_module
from dynfarm import registry
from dynfarm.cli import cli
from dynfarm.context import AppContext
from dynfarm.errors import ConfigError, FarmError
from dynfarm.http import FarmClient, needs_client, parse_retry_after, segment

FAKE_TOKEN = "tok-secret-for-leak-checks"


def reply(handler):
    """A transport that answers with `handler(request)`, for answers the fake farm would never give."""
    return httpx.MockTransport(handler)


def client_for(farm, token=ALICE, sleeps=None, **kwargs) -> FarmClient:
    return FarmClient(
        HOST, token, transport=farm.transport(), sleep=(sleeps.append if sleeps is not None else None), **kwargs
    )


def error_reply(status, code="boom", *, retryable=False, headers=None, message="it broke"):
    body = {"error": {"code": code, "message": message, "retryable": retryable}}
    return httpx.Response(status, json=body, headers=headers or {})


def sequence(*answers):
    """A transport that gives the answers in order (an exception instance is raised) and counts the calls."""
    calls: list[httpx.Request] = []
    pending = list(answers)

    def handler(request):
        calls.append(request)
        answer = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    transport = reply(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


# -- requests ---------------------------------------------------------------------------------------------------


def test_a_request_goes_under_the_api_prefix_with_the_bearer_token_and_a_user_agent(fake_farm):
    with client_for(fake_farm) as client:
        who = client.request("GET", "/whoami", operation_id="whoami")
    assert who["subject"] == "alice"
    sent = fake_farm.requests[0]
    assert sent.path == "/farm/api/v1/whoami"
    assert sent.headers["authorization"] == f"Bearer {ALICE}"
    assert sent.headers["user-agent"].startswith("dynfarm/")


def test_the_token_is_only_ever_in_the_authorization_header(fake_farm):
    with client_for(fake_farm) as client:
        client.request("GET", "/tasks", params={"limit": 5})
        client.request("POST", "/tasks", json={"agent_id": "repo-helper"})
    for sent in fake_farm.requests:
        assert ALICE not in sent.target
        assert ALICE not in sent.body.decode()
        assert [name for name, value in sent.headers.items() if ALICE in value] == ["authorization"]


def test_unset_query_options_are_dropped_and_booleans_are_spelled_for_fastapi(fake_farm):
    seen = []
    transport = reply(lambda request: seen.append(request.url.query.decode()) or httpx.Response(200, json={}))
    with FarmClient(HOST, ALICE, transport=transport) as client:
        client.request("GET", "/tasks", params={"state": None, "limit": 3, "flag": True, "off": False})
    assert seen == ["limit=3&flag=true&off=false"]


def test_a_path_segment_is_escaped_so_an_id_cannot_reach_another_route():
    assert segment("a/b") == "a%2Fb"
    assert segment("..") == ".."
    assert segment("../credentials") == "..%2Fcredentials"
    assert segment("a b") == "a%20b"


def test_an_empty_reply_is_none_and_a_reply_that_is_not_json_is_response_invalid(fake_farm):
    with FarmClient(HOST, ALICE, transport=reply(lambda r: httpx.Response(204))) as client:
        assert client.request("DELETE", "/tasks/x") is None
    with FarmClient(HOST, ALICE, transport=reply(lambda r: httpx.Response(200, text="<html>hi</html>"))) as client:
        with pytest.raises(FarmError) as caught:
            client.request("GET", "/whoami")
    assert caught.value.code == "response_invalid"


def test_a_redirect_is_not_followed_and_is_reported_as_an_http_error():
    transport = sequence(httpx.Response(302, headers={"Location": "https://elsewhere.example/"}))
    with FarmClient(HOST, ALICE, transport=transport) as client:
        with pytest.raises(FarmError) as caught:
            client.request("GET", "/whoami")
    assert (caught.value.status, caught.value.code) == (302, "http_error")
    assert len(transport.calls) == 1


# -- the token never travels in clear ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["http://farm.example.com", "http://10.0.0.5:8000", "http://localhost.evil.example"])
def test_a_client_refuses_plain_http_to_a_host_that_is_not_loopback(host):
    with pytest.raises(ConfigError) as caught:
        FarmClient(host, FAKE_TOKEN)
    assert caught.value.exit_code == 2
    assert FAKE_TOKEN not in caught.value.message


@pytest.mark.parametrize("host", ["http://127.0.0.1:8080", "http://localhost:9", "http://[::1]:8080"])
def test_plain_http_is_accepted_for_loopback(host):
    FarmClient(host, FAKE_TOKEN).close()


@pytest.mark.parametrize("token", ["tok with space", "tok\nsecond-line", "tok\x00", "tök"])
def test_a_token_that_cannot_sit_in_a_header_is_refused_without_repeating_it(token):
    with pytest.raises(ConfigError) as caught:
        FarmClient(HOST, token)
    assert token.strip() not in caught.value.message


def test_a_farm_that_echoes_the_token_in_its_message_gets_it_masked():
    body = {"error": {"code": "weird", "message": f"bad key {FAKE_TOKEN}", "details": {"key": [FAKE_TOKEN]}}}
    with FarmClient(HOST, FAKE_TOKEN, transport=reply(lambda r: httpx.Response(400, json=body))) as client:
        with pytest.raises(FarmError) as caught:
            client.request("GET", "/whoami")
    assert FAKE_TOKEN not in caught.value.message
    assert FAKE_TOKEN not in str(caught.value.details)
    assert "[redacted]" in caught.value.message


# -- errors -----------------------------------------------------------------------------------------------------


def test_the_error_envelope_becomes_a_farm_error_with_its_code_details_and_flags(fake_farm):
    with client_for(fake_farm) as client:
        with pytest.raises(FarmError) as caught:
            client.request("GET", "/tasks/not-a-task")
    error = caught.value
    assert (error.status, error.code, error.retryable) == (404, "task_not_found", False)
    assert error.exit_code == 1


def test_an_expired_token_and_an_invalid_one_are_told_apart_and_both_exit_4(fake_farm):
    for token, code in ((EXPIRED, "token_expired"), ("tok-nobody", "token_invalid")):
        with client_for(fake_farm, token) as client:
            with pytest.raises(FarmError) as caught:
                client.request("GET", "/whoami")
        assert caught.value.code == code
        assert caught.value.exit_code == 4
        assert caught.value.details.get("expires_at") == ("2026-09-30T08:00:00Z" if code == "token_expired" else None)


def test_the_two_authentication_failures_say_different_things():
    expired = FarmError(401, "token_expired", "x").render()
    invalid = FarmError(401, "token_invalid", "x").render()
    assert "expired" in expired[0] and "dynfarm auth login" in expired[0]
    assert "rejected" in invalid[0] and "expired" not in invalid[0]


def test_a_401_without_an_envelope_is_classified_by_the_www_authenticate_header():
    expired = httpx.Response(
        401, headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="expired"'}
    )
    for answer, code in ((expired, "token_expired"), (httpx.Response(401), "token_invalid")):
        with FarmClient(HOST, ALICE, transport=sequence(answer)) as client:
            with pytest.raises(FarmError) as caught:
                client.request("GET", "/whoami")
        assert caught.value.code == code


def test_a_frameworks_422_becomes_request_invalid_and_never_repeats_the_input():
    detail = [{"loc": ["body", "value"], "msg": "Field required", "type": "missing", "input": {"value": FAKE_TOKEN}}]
    with FarmClient(HOST, ALICE, transport=sequence(httpx.Response(422, json={"detail": detail}))) as client:
        with pytest.raises(FarmError) as caught:
            client.request("POST", "/credentials", json={})
    error = caught.value
    assert error.code == "request_invalid"
    assert "body.value: Field required" in error.message
    assert FAKE_TOKEN not in error.message + str(error.details)


def test_a_reply_that_is_not_an_envelope_is_reported_by_status_only():
    with FarmClient(
        HOST, ALICE, transport=sequence(httpx.Response(502, text="<html>Bad gateway SECRET</html>"))
    ) as client:
        with pytest.raises(FarmError) as caught:
            client.request("GET", "/whoami")
    assert (caught.value.status, caught.value.code, caught.value.retryable) == (502, "http_error", False)
    assert "SECRET" not in caught.value.message


def test_the_message_of_a_retryable_error_says_when_to_try_again():
    assert FarmError(429, "quota_exceeded", "full", retryable=True, retry_after=2.2).render() == [
        "error: full (quota_exceeded)",
        "retryable: try again in 3 s",
    ]
    assert FarmError(503, "x", "down", retryable=True).render()[1] == "retryable: try again"
    assert len(FarmError(404, "x", "gone").render()) == 1


# -- retries ----------------------------------------------------------------------------------------------------


def test_a_get_is_retried_after_the_farms_retry_after_when_the_error_is_retryable(fake_farm):
    sleeps: list[float] = []
    fake_farm.fail_next("whoami", status=503, code="database_busy", retry_after=2)
    with client_for(fake_farm, sleeps=sleeps) as client:
        assert client.request("GET", "/whoami")["subject"] == "alice"
    assert sleeps == [2.0]
    assert len(fake_farm.requests) == 2


def test_the_wait_is_capped_at_30_seconds_and_the_error_still_reports_the_farms_number():
    sleeps: list[float] = []
    busy = error_reply(503, "database_busy", retryable=True, headers={"Retry-After": "3600"})
    transport = sequence(busy, busy, busy)
    with FarmClient(HOST, ALICE, transport=transport, sleep=sleeps.append) as client:
        with pytest.raises(FarmError) as caught:
            client.request("GET", "/whoami")
    assert sleeps == [30.0, 30.0]
    assert len(transport.calls) == 3
    assert caught.value.retry_after == 3600


def test_without_a_retry_after_the_wait_grows_and_there_are_three_attempts_at_most():
    sleeps: list[float] = []
    transport = sequence(error_reply(503, "cluster_unavailable", retryable=True))
    with FarmClient(HOST, ALICE, transport=transport, sleep=sleeps.append) as client:
        with pytest.raises(FarmError):
            client.request("GET", "/whoami")
    assert sleeps == [0.5, 1.0]
    assert len(transport.calls) == 3


def test_an_error_that_is_not_retryable_is_not_retried_even_on_a_get():
    transport = sequence(error_reply(404, "task_not_found"))
    with FarmClient(HOST, ALICE, transport=transport, sleep=lambda s: None) as client:
        with pytest.raises(FarmError):
            client.request("GET", "/tasks/x")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_a_write_is_never_retried_by_itself_but_the_error_says_it_can_be_repeated(method):
    sleeps: list[float] = []
    transport = sequence(error_reply(429, "quota_exceeded", retryable=True, headers={"Retry-After": "5"}))
    with FarmClient(HOST, ALICE, transport=transport, sleep=sleeps.append) as client:
        with pytest.raises(FarmError) as caught:
            client.request(method, "/tasks", json={})
    assert len(transport.calls) == 1
    assert sleeps == []
    assert caught.value.retryable is True
    assert caught.value.render()[1] == "retryable: try again in 5 s"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("7", 7.0), (" 12 ", 12.0), ("0", 0.0), (None, None), ("", None), ("soon", None), ("-3", None), ("1.5", None)],
)
def test_retry_after_in_seconds_is_read_and_garbage_is_ignored(value, expected):
    assert parse_retry_after(value) == expected


def test_retry_after_as_an_http_date_is_a_delay_and_a_past_date_is_zero():
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert parse_retry_after("Fri, 01 Jan 2100 00:00:00 GMT") > 1e6


# -- network failures -------------------------------------------------------------------------------------------


def test_a_get_that_cannot_connect_is_retried_then_reported_as_connection_failed():
    sleeps: list[float] = []
    transport = sequence(httpx.ConnectError("[Errno 61] Connection refused"))
    with FarmClient(HOST, ALICE, transport=transport, sleep=sleeps.append) as client:
        with pytest.raises(FarmError) as caught:
            client.request("GET", "/whoami")
    assert (caught.value.code, caught.value.retryable) == ("connection_failed", True)
    assert "Connection refused" in caught.value.message
    assert len(transport.calls) == 3


def test_a_post_that_could_not_connect_is_safe_to_repeat_but_is_not_repeated_by_itself():
    transport = sequence(httpx.ConnectError("refused"))
    with FarmClient(HOST, ALICE, transport=transport, sleep=lambda s: None) as client:
        with pytest.raises(FarmError) as caught:
            client.request("POST", "/tasks", json={})
    assert caught.value.retryable is True
    assert len(transport.calls) == 1


def test_a_post_that_timed_out_while_reading_is_not_called_retryable_because_it_may_have_been_processed():
    transport = sequence(httpx.ReadTimeout("slow"))
    with FarmClient(HOST, ALICE, transport=transport) as client:
        with pytest.raises(FarmError) as caught:
            client.request("POST", "/tasks", json={})
    assert caught.value.code == "connection_failed"
    assert caught.value.retryable is False
    assert "may have received the request" in caught.value.message


# -- debug ------------------------------------------------------------------------------------------------------


def test_debug_prints_method_path_status_and_time_but_never_a_header_or_a_body(fake_farm, capsys):
    body = {"name": "n", "type": "generic", "value": "SENTINEL-DEBUG"}
    with client_for(fake_farm, debug=True) as client:
        client.request("POST", "/credentials", json=body, operation_id="write_credential")
        client.request("GET", "/whoami")
    err = capsys.readouterr().err
    assert "[debug] POST /farm/api/v1/credentials (write_credential) -> 201 in " in err
    assert "[debug] GET /farm/api/v1/whoami -> 200" in err
    for forbidden in (ALICE, "SENTINEL-DEBUG", "Bearer", "authorization", "alice"):
        assert forbidden not in err


def test_debug_is_off_by_default_and_on_with_the_environment_variable(fake_farm, capsys, monkeypatch):
    with client_for(fake_farm) as client:
        client.request("GET", "/whoami")
    assert capsys.readouterr().err == ""
    monkeypatch.setenv("DYNFARM_DEBUG", "1")
    with client_for(fake_farm) as client:
        client.request("GET", "/whoami")
    assert "-> 200" in capsys.readouterr().err


def test_debug_shows_a_retry_without_its_body(fake_farm, capsys):
    fake_farm.fail_next("whoami", retry_after=1)
    with client_for(fake_farm, sleeps=[], debug=True) as client:
        client.request("GET", "/whoami")
    err = capsys.readouterr().err
    assert "-> database_busy; retrying in 1 s (2/3)" in err


# -- commands: @needs_client through the real group -------------------------------------------------------------


@click.command()
@click.option("--json", "json_flag", is_flag=True)
@needs_client
def ping(client, json_flag):
    """A test-only command: prints who the token is."""
    click.echo(client.request("GET", "/whoami")["subject"])


@click.command()
@needs_client
def launch(client):
    """A test-only command that launches a task."""
    client.request("POST", "/tasks", json={"agent_id": "repo-helper"})


@click.command()
@needs_client(require_token=False)
def anonymous(client):
    """A test-only command that does not need a token to start."""
    client.request("GET", "/whoami")


@pytest.fixture(autouse=True)
def _test_commands(monkeypatch):
    monkeypatch.setitem(cli.lazy_commands, "anonymous", f"{__name__}:anonymous")
    monkeypatch.setitem(cli.lazy_commands, "ping", f"{__name__}:ping")
    monkeypatch.setitem(cli.lazy_commands, "launch", f"{__name__}:launch")


def test_a_command_gets_a_client_for_the_resolved_farm_and_token(run_cli, fake_farm):
    result = run_cli(["ping"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "alice\n"
    assert fake_farm.requests[0].headers["authorization"] == f"Bearer {ALICE}"


def test_without_a_token_a_command_stops_with_exit_4_before_sending_anything(run_cli, fake_farm):
    result = run_cli(["ping"], token=None)
    assert result.exit_code == 4
    assert "no token for https://farm.test" in result.stderr
    assert "dynfarm auth login" in result.stderr
    assert fake_farm.requests == []


def test_an_expired_token_exits_4_and_says_to_log_in_again(run_cli):
    result = run_cli(["ping"], token=EXPIRED)
    assert result.exit_code == 4
    assert result.stderr.startswith("error: your token expired")
    assert "(token_expired)" in result.stderr


def test_an_invalid_token_exits_4_and_says_the_farm_rejected_it(run_cli):
    result = run_cli(["ping"], token="tok-nobody")
    assert result.exit_code == 4
    assert result.stderr == "error: the farm rejected your token (token_invalid)\n"


def test_an_api_error_exits_1_with_the_message_and_the_code(run_cli, fake_farm):
    fake_farm.fail_next("whoami", status=404, code="task_not_found", message="no such task", retryable=False)
    result = run_cli(["ping"])
    assert result.exit_code == 1
    assert result.stderr == "error: no such task (task_not_found)\n"


def test_a_failed_write_prints_that_it_can_be_repeated_and_exits_1(run_cli, fake_farm):
    fake_farm.fail_next("create_task", status=429, code="quota_exceeded", retryable=True, retry_after=7)
    result = run_cli(["launch"])
    assert result.exit_code == 1
    assert result.stderr.splitlines() == ["error: the database is busy (quota_exceeded)", "retryable: try again in 7 s"]


def test_hostname_overrides_the_environment_and_plain_http_to_a_remote_host_exits_2(run_cli, fake_farm):
    refused = run_cli(["ping", "--hostname", "http://farm.example.com"])
    assert refused.exit_code == 2
    assert "refusing to send a token over plain http" in refused.stderr
    assert fake_farm.requests == []
    assert run_cli(["ping", "--hostname", "other.example.com"]).exit_code == 0


def test_with_no_farm_configured_a_command_exits_2_and_says_how_to_configure_one(fake_farm):
    app = AppContext(transport=fake_farm.transport(), token_provider=lambda host: ALICE)
    result = CliRunner().invoke(cli, ["ping"], obj=app)
    assert result.exit_code == 2
    assert "no farm configured" in result.stderr
    assert "dynfarm auth login" in result.stderr


def test_without_an_injected_lookup_dynfarm_token_wins_over_the_farm_py_alias(fake_farm, monkeypatch):
    monkeypatch.setenv("DYNFARM_HOST", HOST)
    monkeypatch.setenv("AGENT_FARM_URL", HOST)  # the farm.py variable answers only for farm.py's own farm
    monkeypatch.setenv("AGENT_FARM_TOKEN", "tok-bob")
    monkeypatch.setenv("DYNFARM_TOKEN", ADMIN)
    result = CliRunner().invoke(cli, ["ping"], obj=AppContext(transport=fake_farm.transport()))
    assert result.stdout == "dana\n"
    monkeypatch.delenv("DYNFARM_TOKEN")
    assert CliRunner().invoke(cli, ["ping"], obj=AppContext(transport=fake_farm.transport())).stdout == "bob\n"


def test_the_global_debug_flag_traces_to_stderr_and_leaves_stdout_alone(run_cli):
    result = run_cli(["--debug", "ping"])
    assert result.stdout == "alice\n"
    assert "-> 200" in result.stderr
    assert ALICE not in result.stderr
    assert "-> 200" not in run_cli(["ping"]).stderr


def test_a_command_built_with_needs_client_offers_hostname_in_its_help(run_cli):
    assert "--hostname" in run_cli(["ping", "--help"]).stdout


def test_needs_client_can_be_told_a_command_works_without_a_token(run_cli, fake_farm):
    result = run_cli(["anonymous"], token=None)
    assert result.exit_code == 4
    assert result.stderr == "error: the farm rejected your token (token_invalid)\n"
    assert [r.headers.get("authorization") for r in fake_farm.requests] == [None]


# -- the operation registry -------------------------------------------------------------------------------------


def test_a_command_declares_the_operations_it_calls_and_the_registry_lists_them(monkeypatch):
    monkeypatch.setattr(registry, "_OPERATIONS", {})

    @registry.operation("list_tasks", "get_task")
    def one():
        pass

    @registry.operation("get_task")
    def two():
        pass

    names = registry.registered()
    assert list(names) == ["get_task", "list_tasks"]
    assert [n.rpartition(".")[2] for n in names["get_task"]] == ["one", "two"]
    assert one.__dynfarm_operations__ == ("list_tasks", "get_task")


def test_the_declaration_survives_needs_client_in_either_order(monkeypatch):
    monkeypatch.setattr(registry, "_OPERATIONS", {})
    inner = registry.operation("whoami")(needs_client(lambda client: None))
    outer = needs_client(registry.operation("list_agents")(lambda client: None))
    assert inner.__dynfarm_operations__ == ("whoami",)
    assert outer.__dynfarm_operations__ == ("list_agents",)


@pytest.mark.parametrize("args", [(), ("",), (3,)])
def test_declaring_no_operation_or_a_blank_one_is_a_programming_error(args):
    with pytest.raises(ValueError):
        registry.operation(*args)


def test_load_all_commands_imports_every_module_of_the_command_table(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "dynfarm_fake_commands", types.ModuleType("dynfarm_fake_commands"))
    monkeypatch.setattr(cli_module, "COMMANDS", {"fake": "dynfarm_fake_commands:group"})
    registry.load_all_commands()
    monkeypatch.setattr(cli_module, "COMMANDS", {"gone": "dynfarm_not_a_module:group"})
    with pytest.raises(ModuleNotFoundError):
        registry.load_all_commands()
