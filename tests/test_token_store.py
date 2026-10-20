"""Where a token is kept and found: the precedence a script relies on, and the places a token must never end up.

`dynfarm.tokens` is the only code that reads or writes a stored token, so its promises are pinned here: the
environment beats the keyring, which beats the `0600` file; the file is used only when asked for or when no keyring can
keep the token, and then with a warning; with `CI` set the keyring is not touched at all; and an error from a keyring
backend never carries its text (which could quote the token) into a warning or an exception. Every test runs against
an in-memory keyring (`conftest.memory_keyring`), never the real one.
"""

from __future__ import annotations

import os
import stat
import tomllib

import click
import pytest
from click.testing import CliRunner
from fakefarm import ALICE, BOB, HOST
from keyring.backend import KeyringBackend
from keyring.backends import chainer, fail, null
from keyring.errors import KeyringLocked

from dynfarm import _brand
from dynfarm.cli import cli
from dynfarm.config import config_path
from dynfarm.context import AppContext
from dynfarm.http import needs_client
from dynfarm.tokens import KeyringUnavailable, Resolved, TokenStore, token_for

SERVICE = _brand.KEYRING_SERVICE
SENTINEL = "tok-sentinel-5be1c0de"
ANOTHER = "https://another-farm.example"
CUSTOMER = "https://customer-farm.example"


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def store(warnings) -> TokenStore:
    return TokenStore(warn=warnings.append)


def read_config() -> dict:
    return tomllib.loads(config_path().read_text(encoding="utf-8"))


class BrokenKeyring(KeyringBackend):
    """A keyring that is there but fails, quoting what it was given, as some backends do in their messages."""

    priority = 5  # type: ignore[assignment]

    def get_password(self, service, username, password=None):
        raise KeyringLocked(f"cannot unlock the keychain for {service}/{username}: {password}")

    def set_password(self, service, username, password):
        raise KeyringLocked(f"cannot write {password} for {username}")

    delete_password = get_password


class ForgetfulKeyring(KeyringBackend):
    """A keyring that accepts every write and keeps nothing, without looking like the null backend."""

    priority = 5  # type: ignore[assignment]

    def get_password(self, service, username):
        return None

    def set_password(self, service, username, password):
        pass

    def delete_password(self, service, username):
        pass


# -- precedence --------------------------------------------------------------------------------------------------


def test_dynfarm_token_wins_over_the_keyring_and_the_file(store, memory_keyring, monkeypatch):
    store.remember("https://other.test", BOB, insecure=True)
    memory_keyring.entries[(SERVICE, HOST)] = BOB
    monkeypatch.setenv("DYNFARM_TOKEN", ALICE)
    assert store.resolve(HOST) == Resolved(ALICE, "env:DYNFARM_TOKEN")


def test_another_tools_token_variable_is_not_read_whatever_farm_it_is_set_for(store, memory_keyring, monkeypatch):
    monkeypatch.setenv("AGENT_FARM_TOKEN", ALICE)  # another tool's variable: only DYNFARM_* are read
    memory_keyring.entries[(SERVICE, CUSTOMER)] = BOB
    assert store.resolve(ANOTHER) is None
    assert store.resolve(HOST) is None
    assert store.resolve(CUSTOMER) == Resolved(BOB, "keyring")
    monkeypatch.setenv("AGENT_FARM_URL", HOST)
    assert store.resolve(HOST) is None


def test_dynfarm_token_answers_for_any_farm(store, monkeypatch):
    monkeypatch.setenv("DYNFARM_TOKEN", ALICE)
    for host in (HOST, CUSTOMER, ANOTHER):
        assert store.resolve(host) == Resolved(ALICE, "env:DYNFARM_TOKEN")


def test_a_blank_environment_variable_counts_as_unset(store, memory_keyring, monkeypatch):
    memory_keyring.entries[(SERVICE, HOST)] = ALICE
    monkeypatch.setenv("DYNFARM_TOKEN", "   ")
    assert store.resolve(HOST) == Resolved(ALICE, "keyring")


def test_the_keyring_wins_over_the_file(store, memory_keyring):
    store.remember(HOST, BOB, insecure=True)
    memory_keyring.entries[(SERVICE, HOST)] = ALICE
    assert store.resolve(HOST) == Resolved(ALICE, "keyring")


def test_the_file_answers_when_the_keyring_has_nothing(store):
    store.remember(HOST, BOB, insecure=True)
    assert store.resolve(HOST) == Resolved(BOB, "file")


def test_nothing_stored_anywhere_resolves_to_none(store):
    assert store.resolve(HOST) is None


def test_two_farms_keep_two_tokens_under_their_normalised_urls(store, memory_keyring):
    store.remember("Farm.Test/some/path", ALICE)
    store.remember("https://other.test:8443/", BOB)
    assert memory_keyring.entries == {(SERVICE, "https://farm.test"): ALICE, (SERVICE, "https://other.test:8443"): BOB}
    assert store.resolve("https://FARM.test").token == ALICE
    assert store.resolve("other.test:8443").token == BOB


def test_the_resolved_token_is_not_in_its_repr():
    assert SENTINEL not in repr(Resolved(SENTINEL, "keyring"))


# -- where remember puts it -------------------------------------------------------------------------------------


def test_remember_puts_the_token_in_the_keyring_and_only_the_farm_in_the_file(store, memory_keyring, warnings):
    saved = store.remember(HOST, SENTINEL)
    assert saved.where == "keyring"
    assert memory_keyring.entries[(SERVICE, HOST)] == SENTINEL
    assert SENTINEL not in config_path().read_text(encoding="utf-8")
    assert read_config() == {"default_host": HOST, "hosts": {HOST: {}}}
    assert warnings == []


@pytest.mark.parametrize(
    "backend",
    [fail.Keyring(), null.Keyring(), chainer.ChainerBackend()],
    ids=["fail-backend", "null-backend", "empty-chain"],
)
def test_with_no_usable_keyring_the_token_goes_to_a_0600_file_with_a_warning(
    store, warnings, use_keyring, backend, monkeypatch
):
    if isinstance(backend, chainer.ChainerBackend):
        monkeypatch.setattr(chainer.ChainerBackend, "backends", [])
    use_keyring(backend)
    saved = store.remember(HOST, SENTINEL)
    assert saved.where == "file"
    assert read_config()["hosts"][HOST]["token"] == SENTINEL
    if os.name == "posix":
        assert stat.S_IMODE(config_path().stat().st_mode) == 0o600
    assert len(warnings) == 1
    assert "plain text" in warnings[0] and str(config_path()) in warnings[0]
    assert SENTINEL not in warnings[0]
    assert store.resolve(HOST) == Resolved(SENTINEL, "file")


def test_insecure_storage_writes_the_file_even_with_a_keyring_and_clears_the_old_keyring_entry(
    store, memory_keyring, warnings
):
    memory_keyring.entries[(SERVICE, HOST)] = BOB
    saved = store.remember(HOST, ALICE, insecure=True)
    assert saved.where == "file"
    assert (SERVICE, HOST) not in memory_keyring.entries
    assert store.resolve(HOST) == Resolved(ALICE, "file")
    assert warnings == []


def test_a_token_saved_to_the_keyring_later_removes_the_plain_text_copy(store):
    store.remember(HOST, BOB, insecure=True)
    store.remember(HOST, ALICE)
    assert "token" not in read_config()["hosts"][HOST]
    assert store.resolve(HOST) == Resolved(ALICE, "keyring")


def test_a_keyring_that_accepts_a_write_but_keeps_nothing_is_not_trusted(store, warnings, use_keyring):
    use_keyring(ForgetfulKeyring())
    assert store.remember(HOST, ALICE).where == "file"
    assert "did not keep the token" in warnings[0]


def test_the_first_farm_remembered_becomes_the_default_and_the_second_does_not(store):
    assert store.remember(HOST, ALICE).made_default is True
    assert store.remember("https://other.test", BOB).made_default is False
    assert store.default_host() == HOST
    assert store.hosts() == [HOST, "https://other.test"]


def test_forget_removes_the_keyring_entry_and_the_file_token_and_moves_the_default(store, memory_keyring):
    store.remember(HOST, ALICE, insecure=True)
    store.remember("https://other.test", BOB)
    memory_keyring.entries[(SERVICE, HOST)] = ALICE  # left over from an earlier keyring login
    removed, default = store.forget(HOST)
    assert removed == ["keyring", "file"]
    assert default == "https://other.test"
    assert store.resolve(HOST) is None
    assert read_config() == {"default_host": "https://other.test", "hosts": {"https://other.test": {}}}
    assert store.forget("https://other.test") == (["keyring"], None)
    assert read_config() == {}


# -- CI and broken keyrings ---------------------------------------------------------------------------------------


def test_with_ci_set_the_keyring_is_never_touched(store, memory_keyring, warnings, monkeypatch):
    monkeypatch.setenv("CI", "true")
    memory_keyring.entries[(SERVICE, HOST)] = BOB
    assert store.resolve(HOST) is None
    saved = store.remember(HOST, ALICE)
    assert saved.where == "file"
    assert "CI is set" in warnings[0]
    assert store.resolve(HOST) == Resolved(ALICE, "file")
    assert store.has_stored_token(HOST) is True
    store.forget(HOST)
    assert memory_keyring.calls == []


def test_ci_set_to_false_still_uses_the_keyring(store, memory_keyring, monkeypatch):
    monkeypatch.setenv("CI", "false")
    assert store.remember(HOST, ALICE).where == "keyring"
    assert memory_keyring.calls


def test_a_keyring_error_never_carries_its_text_into_a_warning_or_an_exception(store, warnings, use_keyring):
    use_keyring(BrokenKeyring())
    store.remember(HOST, SENTINEL)
    assert store.resolve(HOST) == Resolved(SENTINEL, "file")
    store.forget(HOST)
    assert warnings, "a failing keyring must be reported"
    assert all("KeyringLocked" in line and SENTINEL not in line for line in warnings)
    with pytest.raises(KeyringUnavailable) as caught:
        store._call("set_password", SERVICE, HOST, SENTINEL)
    assert SENTINEL not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_a_keyring_that_cannot_be_read_falls_back_to_the_file_with_a_warning(store, warnings, use_keyring):
    store.remember(HOST, ALICE, insecure=True)
    use_keyring(BrokenKeyring())
    assert store.resolve(HOST) == Resolved(ALICE, "file")
    assert warnings == [f"the system keyring could not be read (KeyringLocked); trying {config_path()}"]


def test_a_missing_keyring_is_silent_when_reading(store, warnings, use_keyring):
    use_keyring(fail.Keyring())
    assert store.resolve(HOST) is None
    assert warnings == []


# -- the provider commands use -------------------------------------------------------------------------------------


@click.command()
@needs_client
def who(client):
    """A test-only command: prints whose token was used."""
    click.echo(client.request("GET", "/whoami")["subject"])


def test_a_command_with_no_injected_lookup_uses_the_stored_token(fake_farm, memory_keyring, monkeypatch):
    monkeypatch.setitem(cli.lazy_commands, "who", f"{__name__}:who")
    monkeypatch.setenv("DYNFARM_HOST", HOST)
    memory_keyring.entries[(SERVICE, HOST)] = BOB
    result = CliRunner().invoke(cli, ["who"], obj=AppContext(transport=fake_farm.transport()))
    assert result.exit_code == 0, result.output
    assert result.stdout == "bob\n"
    assert token_for(HOST) == BOB


@pytest.mark.parametrize("farm", [CUSTOMER, ANOTHER])
def test_another_tools_token_variable_is_sent_to_no_farm_whichever_it_is(fake_farm, monkeypatch, farm):
    monkeypatch.setitem(cli.lazy_commands, "who", f"{__name__}:who")
    monkeypatch.setenv("AGENT_FARM_TOKEN", ALICE)  # exported in a shell profile for another tool
    result = CliRunner().invoke(cli, ["who", "--hostname", farm], obj=AppContext(transport=fake_farm.transport()))
    assert result.exit_code == 4
    assert f"no token for {farm}" in result.stderr
    assert fake_farm.requests == []
    assert all("authorization" not in r.headers for r in fake_farm.requests)
    assert ALICE not in result.output
