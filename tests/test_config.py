"""Which farm a command talks to, where the settings live, and that a token is never sent in clear.

These are the rules every command inherits: hosts are written one way (so a keyring account and a config table agree),
resolution is `--hostname` > `DYNFARM_HOST` > the config's default and fails closed when nothing answers (there is
no built-in farm; another tool's URL variable is not read), plain `http` is refused unless loopback, and the config
file, which can hold a token, is private and replaced atomically.
"""

from __future__ import annotations

import os
import stat

import platformdirs
import pytest

from dynfarm import config
from dynfarm.config import is_loopback, normalize_host, resolve_host
from dynfarm.errors import ConfigError

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("farm.example.com", "https://farm.example.com"),
        ("https://Farm.Example.COM/", "https://farm.example.com"),
        ("https://farm.example.com/farm/api/v1?x=1#frag", "https://farm.example.com"),
        ("  farm.example.com  ", "https://farm.example.com"),
        ("https://farm.example.com:443", "https://farm.example.com"),
        ("https://farm.example.com:8443/path", "https://farm.example.com:8443"),
        ("http://localhost:8080/", "http://localhost:8080"),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://[::1]:9000", "http://[::1]:9000"),
        ("localhost", "https://localhost"),
        ("HTTP://LOCALHOST", "http://localhost"),
    ],
)
def test_a_host_is_written_one_way_whatever_the_person_typed(raw, expected):
    assert normalize_host(raw) == expected


def test_normalising_twice_changes_nothing():
    once = normalize_host("Farm.Example.com/x")
    assert normalize_host(once) == once


@pytest.mark.parametrize("raw", ["http://farm.example.com", "http://10.0.0.1:8000", "http://localhost.example.com"])
def test_plain_http_is_refused_for_a_host_that_is_not_loopback(raw):
    with pytest.raises(ConfigError) as caught:
        normalize_host(raw)
    assert caught.value.exit_code == 2
    assert "plain http" in caught.value.message


@pytest.mark.parametrize(
    "raw", ["", "   ", "ftp://farm.example.com", "https://", "https://farm.example.com:notaport", "https://[::1"]
)
def test_nonsense_is_a_configuration_error(raw):
    with pytest.raises(ConfigError):
        normalize_host(raw)


def test_a_url_carrying_credentials_is_refused_and_the_message_does_not_repeat_them():
    with pytest.raises(ConfigError) as caught:
        normalize_host("https://tok-very-secret@farm.example.com")
    assert "tok-very-secret" not in caught.value.message
    with pytest.raises(ConfigError) as caught:
        normalize_host("https://user:tok-very-secret@farm.example.com")
    assert "tok-very-secret" not in caught.value.message


def test_only_the_three_loopback_names_count_as_loopback():
    assert [is_loopback(h) for h in ("http://localhost", "http://127.0.0.1:1", "http://[::1]")] == [True] * 3
    assert not is_loopback("https://farm.example.com")
    assert not is_loopback("http://127.0.0.2")


# -- resolution -------------------------------------------------------------------------------------------------


def write_default(host: str) -> None:
    config.save_config({"default_host": host, "hosts": {host: {}}})


def test_the_flag_beats_the_environment_which_beats_the_config(monkeypatch):
    write_default("https://from-config.example.com")
    assert resolve_host() == "https://from-config.example.com"
    monkeypatch.setenv("DYNFARM_HOST", "from-env.example.com")
    assert resolve_host() == "https://from-env.example.com"
    assert resolve_host("from-flag.example.com") == "https://from-flag.example.com"


def test_another_tools_url_variable_is_not_read_so_it_neither_beats_the_config_nor_names_a_farm(monkeypatch):
    monkeypatch.setenv("AGENT_FARM_URL", "from-another-tool.example.com")
    with pytest.raises(ConfigError) as caught:
        resolve_host()
    assert caught.value.exit_code == 2
    assert caught.value.message == "no farm configured — run `dynfarm auth login`"
    write_default("https://from-config.example.com")
    assert resolve_host() == "https://from-config.example.com"


def test_an_empty_variable_counts_as_unset(monkeypatch):
    write_default("https://from-config.example.com")
    monkeypatch.setenv("DYNFARM_HOST", "  ")
    assert resolve_host("") == "https://from-config.example.com"


def test_with_nothing_configured_it_fails_closed_and_says_how_to_fix_it():
    with pytest.raises(ConfigError) as caught:
        resolve_host()
    assert caught.value.exit_code == 2
    assert caught.value.message == "no farm configured — run `dynfarm auth login`"


def test_a_bad_value_names_where_it_came_from(monkeypatch):
    monkeypatch.setenv("DYNFARM_HOST", "http://farm.example.com")
    with pytest.raises(ConfigError, match=r"^DYNFARM_HOST: refusing"):
        resolve_host()
    with pytest.raises(ConfigError, match=r"^--hostname: "):
        resolve_host("ftp://x")


def test_the_flag_is_enough_even_when_the_config_file_is_broken(config_dir):
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("this is [not toml", encoding="utf-8")
    assert resolve_host("farm.example.com") == "https://farm.example.com"
    with pytest.raises(ConfigError):
        resolve_host()


def test_a_default_host_in_the_file_is_checked_like_any_other():
    config.save_config({"default_host": "http://farm.example.com"})
    with pytest.raises(ConfigError, match="default_host in config.toml"):
        resolve_host()


# -- the file ---------------------------------------------------------------------------------------------------


def test_the_config_directory_is_the_variable_else_the_os_per_user_dir(config_dir, tmp_path):
    assert config.config_dir() == config_dir
    assert config.config_path() == config_dir / "config.toml"
    assert config.config_dir({"DYNFARM_CONFIG_DIR": str(tmp_path / "x")}) == tmp_path / "x"
    assert str(config.config_dir({})) == platformdirs.user_config_dir("dynfarm")


def test_a_missing_config_file_is_an_empty_config():
    assert config.load_config() == {}


def test_what_is_saved_is_what_is_loaded_including_a_token_in_a_host_table():
    data = {
        "default_host": "https://a.example.com",
        "hosts": {"https://a.example.com": {"token": "tok-plain"}, "https://b.example.com": {}},
    }
    path = config.save_config(data)
    assert path.read_text(encoding="utf-8").count("[hosts.") == 2
    assert config.load_config() == data


@posix_only
def test_the_file_is_private_and_so_is_the_directory_it_created(config_dir):
    path = config.save_config({"default_host": "https://a.example.com"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700


@posix_only
def test_rewriting_keeps_the_file_private_whatever_the_umask(config_dir):
    old = os.umask(0o000)
    try:
        config.save_config({"default_host": "https://a.example.com"})
        path = config.save_config({"default_host": "https://b.example.com"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_old_file_whole_and_no_temp_file_behind(config_dir, monkeypatch):
    config.save_config({"default_host": "https://old.example.com"})

    def explode(*args, **kwargs):
        raise OSError("disk on fire")

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", explode)
        with pytest.raises(OSError, match="disk on fire"):
            config.save_config({"default_host": "https://new.example.com"})
    assert config.load_config()["default_host"] == "https://old.example.com"
    assert [p.name for p in config_dir.iterdir()] == ["config.toml"]


def test_a_file_that_is_not_toml_is_an_error_naming_the_file_not_its_content(config_dir):
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('token = "tok-must-not-leak\nbroken [', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        config.load_config()
    assert "config.toml" in caught.value.message
    assert "tok-must-not-leak" not in caught.value.message


@pytest.mark.parametrize("text", ["default_host = 3", "hosts = 'x'", "[hosts]\n'https://a.example.com' = 1"])
def test_a_file_of_the_wrong_shape_is_a_configuration_error(config_dir, text):
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="wrong shape"):
        config.load_config()
