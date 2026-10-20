"""Where the tool keeps its settings, which farm a command talks to, and the rule that a token never travels in clear.

Three things live here because every command needs them before it can make a request:

1. **The config file** (`config.toml`, D12): `default_host` and one `[hosts."<url>"]` table per farm. The token is in it
   only for a farm whose person chose plain-text storage (`dynfarm auth login --insecure-storage`); the file is
   therefore `0600` and its directory `0700` on POSIX, and it is replaced atomically (temp file + rename) so a crash
   never leaves half a file that holds a token. The directory is `DYNFARM_CONFIG_DIR`, else the OS's per-user
   config dir.
2. **Host normalisation**: `farm.example.com`, `https://Farm.Example.com/` and `https://farm.example.com/anything`
   are one farm, written one way, so the keyring account, the config table and the comparison all agree.
3. **Host resolution**: `--hostname` > `DYNFARM_HOST` > `AGENT_FARM_URL` (the alias `farm.py` documents, D8) >
   `default_host`. There is no built-in default (D9): the tool serves several farms and a Dynapps URL baked in would be
   wrong for the others. Nothing resolved is a configuration error (exit 2), never a guess.

Plain `http://` is refused for any host that is not loopback (D10). The check is in `normalize_host`, so every road to
a farm URL (flag, environment, file, a client built directly) goes through it, and a token cannot be handed to a
network it would cross in clear.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import platformdirs
import tomli_w

from dynfarm import _brand
from dynfarm.errors import ConfigError

#: The only hosts a token may be sent to over plain `http` (D10): tests and `local-e2e` talk to these.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_host(raw: str) -> str:
    """`farm.x` -> `https://farm.x`: scheme added, host lower-cased, path/query/fragment and default port dropped.

    Raises `ConfigError` for an empty value, a scheme other than http(s), a URL that carries a user name or password
    (a token pasted as `https://<token>@host` must not end up in a URL, a config file or a log) and for plain `http`
    to anything that is not loopback. The error messages never repeat what the person typed, only what was parsed out
    of it: a value pasted into the wrong flag could be a token.
    """
    text = raw.strip()
    if not text:
        raise ConfigError("the farm host is empty")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ConfigError("that is not a valid farm host") from None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ConfigError(f"unsupported scheme '{scheme}': a farm is reached over https (or http on localhost)")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("a farm host must not carry a user name or password; the token is stored separately")
    if not hostname:
        raise ConfigError("that is not a valid farm host")
    if scheme == "http" and hostname not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"refusing to send a token over plain http to {hostname}: use https "
            "(http is accepted only for localhost, 127.0.0.1 and ::1)"
        )
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def is_loopback(host: str) -> bool:
    """Whether a normalised host is on this machine (the only place plain `http` is acceptable)."""
    hostname = urlsplit(host).hostname
    return hostname in LOOPBACK_HOSTS


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """`DYNFARM_CONFIG_DIR` if set, else the OS's per-user config directory for the tool."""
    environ = os.environ if environ is None else environ
    override = environ.get(_brand.env("CONFIG_DIR"), "").strip()
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_config_dir(_brand.NAME))


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    return config_dir(environ) / "config.toml"


def load_config(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The parsed config file; `{}` when there is none. A file that cannot be read is a configuration error.

    The error names the file and the position, never the content: the file may hold a token.
    """
    path = config_path(environ)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc.__class__.__name__}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        position = f" ({exc})" if isinstance(exc, tomllib.TOMLDecodeError) else ""
        raise ConfigError(f"{path} is not valid TOML{position}") from None
    default_host = data.get("default_host")
    hosts = data.get("hosts", {})
    if (default_host is not None and not isinstance(default_host, str)) or not (
        isinstance(hosts, dict) and all(isinstance(entry, dict) for entry in hosts.values())
    ):
        raise ConfigError(f'{path} has the wrong shape: expected `default_host` and `[hosts."<url>"]` tables')
    return data


def save_config(data: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> Path:
    """Write the config file atomically, `0600`, in a `0700` directory (POSIX). Returns its path.

    The temp file is created in the same directory with owner-only permissions *before* anything is written to it, so a
    token is never on disk readable by others, not even for an instant; `os.replace` then swaps it in whole.
    """
    path = config_path(environ)
    directory = path.parent
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if created and os.name == "posix":
        os.chmod(directory, 0o700)
    payload = tomli_w.dumps(dict(data)).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def resolve_host(flag: str | None = None, *, environ: Mapping[str, str] | None = None) -> str:
    """The normalised farm URL a command talks to, or a `ConfigError` saying how to configure one.

    Precedence: `--hostname`, `DYNFARM_HOST`, `AGENT_FARM_URL`, then `default_host` of the config file. An empty value
    counts as unset. The config file is read only if nothing before it answered, so a broken file does not stop a
    command that was told its farm explicitly.
    """
    environ = os.environ if environ is None else environ
    explicit = (
        ("--hostname", flag),
        (_brand.env("HOST"), environ.get(_brand.env("HOST"))),
        (_brand.LEGACY_ENV_HOST, environ.get(_brand.LEGACY_ENV_HOST)),
    )
    for label, value in explicit:
        if value and value.strip():
            return _normalize_from(label, value)
    default_host = load_config(environ).get("default_host")
    if default_host and default_host.strip():
        return _normalize_from("default_host in config.toml", default_host)
    raise ConfigError(f"no farm configured — run `{_brand.NAME} auth login`")


def _normalize_from(label: str, value: str) -> str:
    try:
        return normalize_host(value)
    except ConfigError as exc:
        raise ConfigError(f"{label}: {exc.message}") from None
