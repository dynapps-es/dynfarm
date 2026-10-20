"""Where a farm's token is kept, and where a command finds it.

One token per farm, like `gh`. Looking one up goes, first answer wins:

1. `DYNFARM_TOKEN`: CI and scripts set it, and an explicit environment beats anything stored. It answers for
   whichever farm is targeted (like `GH_TOKEN`). It is the only environment variable that carries a token: the
   variable of another tool is not read, so a token meant for one farm is never handed to another by accident.
2. The OS keyring (Keychain, Windows Credential Manager, Secret Service), service `_brand.KEYRING_SERVICE`, account the
   normalised farm URL, so two farms never share an entry.
3. `token` in the farm's `[hosts."<url>"]` table of `config.toml`, a `0600` file: only for a farm whose person chose
   `--insecure-storage`, or when no keyring is usable (a headless box), and then with a warning on stderr.

When `CI` is set the keyring is never touched, not even to read: a CI runner has no keyring worth trusting, and a
backend that opens a dialog would hang the job.

The token is never put in a message. The keyring libraries' own errors can quote what they were handed, so every
keyring call goes through `_call`, which keeps only the error's class name (`KeyringUnavailable.reason`) and drops the
original exception (`from None`) so not even a traceback can carry its text.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from dynfarm import _brand
from dynfarm.config import config_path, load_config, normalize_host, save_config

#: The environment variable that carries a token.
ENV_TOKEN = _brand.env("TOKEN")

SOURCE_KEYRING = "keyring"
SOURCE_FILE = "file"

_FALSEY = {"", "0", "false", "no", "off"}
#: `reason` of a keyring that is simply not there (no backend), as opposed to one that failed: no warning for it.
NO_KEYRING = "no system keyring"


class KeyringUnavailable(Exception):
    """The keyring could not do what was asked. `reason` is safe to print: a class name or a fixed phrase."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Resolved:
    """A token and where it came from: `env:DYNFARM_TOKEN`, `keyring` or `file`."""

    token: str = field(repr=False)
    source: str


@dataclass(frozen=True)
class Saved:
    """Where `remember` put a token: `keyring` or `file` (then `path` is the config file), and whether the farm became
    the default one."""

    where: str
    path: Path
    made_default: bool


def in_ci(environ: Mapping[str, str]) -> bool:
    """`CI` set to anything but an explicit no (`0`, `false`, ...): every CI system sets it."""
    return environ.get("CI", "").strip().lower() not in _FALSEY


def env_token(environ: Mapping[str, str]) -> Resolved | None:
    """The environment's token, if any; an empty or blank variable counts as unset. It applies to any farm."""
    value = environ.get(ENV_TOKEN, "").strip()
    return Resolved(value, f"env:{ENV_TOKEN}") if value else None


class TokenStore:
    """The tokens of every farm this person logged in to, and the `[hosts]` of `config.toml` they belong to.

    `environ` defaults to `os.environ` (tests pass their own); `warn` prints one line to stderr unless replaced.
    """

    def __init__(self, environ: Mapping[str, str] | None = None, *, warn: Callable[[str], None] | None = None):
        self.environ = os.environ if environ is None else environ
        self._warn = warn or (lambda line: click.echo(f"warning: {line}", err=True))

    # -- reading -----------------------------------------------------------------------------------------------

    def resolve(self, host: str) -> Resolved | None:
        """The token a command uses for `host`, with its source, or None when there is none anywhere."""
        host = normalize_host(host)
        found = env_token(self.environ)
        if found is not None:
            return found
        if not in_ci(self.environ):
            try:
                token = self._call("get_password", _brand.KEYRING_SERVICE, host)
            except KeyringUnavailable as exc:
                if exc.reason != NO_KEYRING:
                    path = config_path(self.environ)
                    self._warn(f"the system keyring could not be read ({exc.reason}); trying {path}")
            else:
                if token:
                    return Resolved(token, SOURCE_KEYRING)
        token = self.file_token(host)
        return Resolved(token, SOURCE_FILE) if token else None

    def file_token(self, host: str) -> str | None:
        entry = self._hosts(load_config(self.environ)).get(normalize_host(host), {})
        token = entry.get("token")
        return token if isinstance(token, str) and token.strip() else None

    def hosts(self) -> list[str]:
        """The farms in `config.toml`, in file order."""
        return list(self._hosts(load_config(self.environ)))

    def default_host(self) -> str | None:
        value = load_config(self.environ).get("default_host")
        return value if isinstance(value, str) and value.strip() else None

    def has_stored_token(self, host: str) -> bool:
        """Whether the keyring or the file holds a token for `host` (the environment is not storage)."""
        host = normalize_host(host)
        if not in_ci(self.environ):
            try:
                if self._call("get_password", _brand.KEYRING_SERVICE, host):
                    return True
            except KeyringUnavailable:
                pass
        return self.file_token(host) is not None

    # -- writing -----------------------------------------------------------------------------------------------

    def remember(self, host: str, token: str, *, insecure: bool = False) -> Saved:
        """Store `token` for `host` and make sure `host` is in `config.toml` (the default one when it is the first).

        The keyring first; the file when `insecure`, when `CI` is set, or when no keyring can keep it (with a warning).
        Whatever store is not used is cleared for this host, so an old token there cannot shadow the new one.
        """
        host = normalize_host(host)
        config = load_config(self.environ)
        entry = self._hosts(config, create=True).setdefault(host, {})
        made_default = not (isinstance(config.get("default_host"), str) and config["default_host"].strip())
        if made_default:
            config["default_host"] = host
        where = SOURCE_FILE
        if not insecure:
            try:
                self._keyring_set(host, token)
            except KeyringUnavailable as exc:
                reason = "CI is set" if exc.reason == "CI" else exc.reason
                self._warn(
                    f"{reason}: the token is stored in plain text in {config_path(self.environ)} "
                    "(readable only by you); use a system keyring to avoid it"
                )
            else:
                where = SOURCE_KEYRING
        if where == SOURCE_KEYRING:
            entry.pop("token", None)
        else:
            entry["token"] = token
            self._keyring_forget(host)
        path = save_config(config, self.environ)
        return Saved(where, path, made_default)

    def forget(self, host: str) -> tuple[list[str], str | None]:
        """Delete `host`'s token from the keyring and drop its table from `config.toml`.

        Returns the stores that held a token (`keyring`, `file`) and the default farm afterwards: when `host` was the
        default, the first farm left becomes it (or there is none).
        """
        host = normalize_host(host)
        removed = [SOURCE_KEYRING] if self._keyring_forget(host) else []
        config = load_config(self.environ)
        hosts = self._hosts(config, create=True)
        entry = hosts.pop(host, None)
        if isinstance(entry, dict) and entry.get("token"):
            removed.append(SOURCE_FILE)
        if config.get("default_host") == host:
            if hosts:
                config["default_host"] = next(iter(hosts))
            else:
                config.pop("default_host", None)
        if not hosts:
            config.pop("hosts", None)
        save_config(config, self.environ)
        return removed, config.get("default_host")

    def set_default(self, host: str) -> None:
        config = load_config(self.environ)
        config["default_host"] = normalize_host(host)
        save_config(config, self.environ)

    # -- the keyring, wrapped ----------------------------------------------------------------------------------

    def _keyring_set(self, host: str, token: str) -> None:
        self._call("set_password", _brand.KEYRING_SERVICE, host, token)
        # Some backends accept a write and keep nothing; a token that cannot be read back was not stored.
        if self._call("get_password", _brand.KEYRING_SERVICE, host) != token:
            raise KeyringUnavailable("the keyring did not keep the token")

    def _keyring_forget(self, host: str) -> bool:
        """Delete `host`'s keyring entry; True if there was one. Never raises: a broken keyring holds nothing usable."""
        if in_ci(self.environ):
            return False
        try:
            if self._call("get_password", _brand.KEYRING_SERVICE, host) is None:
                return False
            self._call("delete_password", _brand.KEYRING_SERVICE, host)
        except KeyringUnavailable as exc:
            if exc.reason != NO_KEYRING:
                self._warn(f"the system keyring entry for {host} could not be deleted ({exc.reason})")
            return False
        return True

    def _call(self, method: str, *args: Any) -> Any:
        """One keyring operation; any failure becomes a `KeyringUnavailable` that carries no text of the original."""
        if in_ci(self.environ):
            raise KeyringUnavailable("CI")
        backend = _usable_backend()
        try:
            return getattr(backend, method)(*args)
        except Exception as exc:  # the backends raise anything (dbus, ctypes, OSError): none of it may escape as is
            raise KeyringUnavailable(type(exc).__name__) from None

    @staticmethod
    def _hosts(config: dict[str, Any], *, create: bool = False) -> dict[str, dict[str, Any]]:
        hosts = config.get("hosts")
        if not isinstance(hosts, dict):
            hosts = {}
            if create:
                config["hosts"] = hosts
        return hosts


def _usable_backend() -> Any:
    """The keyring backend, or `KeyringUnavailable(NO_KEYRING)` when it is one that stores nothing (`fail`, `null`, an
    empty chain), which is what a server without a desktop session gets."""
    try:
        import keyring
        from keyring.backends import chainer, fail, null

        backend = keyring.get_keyring()
    except Exception as exc:
        raise KeyringUnavailable(type(exc).__name__) from None
    if isinstance(backend, fail.Keyring | null.Keyring):
        raise KeyringUnavailable(NO_KEYRING)
    if isinstance(backend, chainer.ChainerBackend) and not backend.backends:
        raise KeyringUnavailable(NO_KEYRING)
    return backend


def token_for(host: str) -> str | None:
    """The token a command uses for `host` (`AppContext.token_provider` when none is injected)."""
    resolved = TokenStore().resolve(host)
    return resolved.token if resolved is not None else None
