"""Fixtures shared by every test: a clean environment, an isolated config dir and keyring, the fake farm, a CLI runner.

Why the first three are `autouse`: a test that forgot to isolate would read the developer's real `DYNFARM_TOKEN`,
write into their real config directory, or put a test token in their real Keychain (which also pops dialogs on macOS).
Isolation is the default; a test has to go out of its way to leave it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import click
import keyring.core
import pytest
from click.testing import CliRunner, Result
from fakefarm import ALICE, FakeFarm
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from dynfarm import _brand
from dynfarm.cli import cli
from dynfarm.context import AppContext


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(_brand.ENV_PREFIX) or name in ("NO_COLOR", "TERM"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI runners set `CI`, which makes the token store skip the keyring; tests set it only when they test that."""
    monkeypatch.delenv("CI", raising=False)


class MemoryKeyring(KeyringBackend):
    """A keyring that lives in a dict and records every call (`calls`), so a test can also prove it was not touched."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str | None]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username))
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", service, username))
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
        if self.entries.pop((service, username), None) is None:
            raise PasswordDeleteError("no such entry")


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    """The keyring every test sees: in memory, empty. `use_keyring(backend)` swaps in another one."""
    backend = MemoryKeyring()
    monkeypatch.setattr(keyring.core, "_keyring_backend", backend)
    return backend


@pytest.fixture
def use_keyring(monkeypatch: pytest.MonkeyPatch) -> Callable[[KeyringBackend], KeyringBackend]:
    """`use_keyring(fail.Keyring())`: make the token store meet another backend (broken, absent, forgetful)."""

    def install(backend: KeyringBackend) -> KeyringBackend:
        monkeypatch.setattr(keyring.core, "_keyring_backend", backend)
        return backend

    return install


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The (not yet created) config directory every test uses: `DYNFARM_CONFIG_DIR` points at it."""
    directory = tmp_path / "config"
    monkeypatch.setenv(_brand.env("CONFIG_DIR"), str(directory))
    return directory


@pytest.fixture
def fake_farm() -> Iterator[FakeFarm]:
    """A fresh fake farm. At teardown it fails the test if the CLI sent something the OpenAPI does not allow."""
    farm = FakeFarm()
    yield farm
    assert not farm.bugs, f"the fake farm broke the contract: {farm.bugs}"
    assert not farm.violations, f"the CLI sent requests the API does not accept: {farm.violations}"


@pytest.fixture
def sleeps() -> list[float]:
    """Every pause the client asked for; the tests never really wait."""
    return []


@pytest.fixture
def run_cli(fake_farm: FakeFarm, sleeps: list[float], monkeypatch: pytest.MonkeyPatch) -> Callable[..., Result]:
    """`run_cli(["task", "list"], input=None)`: run the CLI in-process against the fake farm, as `tok-alice`.

    The host defaults to the fake's, the token to `tok-alice` (override with `token=`, or `token=None` for none).
    `command=` runs another click command (a test-only group) instead of the real `dynfarm`.
    """

    def run(
        args: list[str],
        input: str | None = None,
        *,
        token: str | None = ALICE,
        command: click.Command = cli,
        env: dict[str, str] | None = None,
    ) -> Result:
        monkeypatch.setenv(_brand.env("HOST"), fake_farm.base_url)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        app = AppContext(
            transport=fake_farm.transport(),
            token_provider=lambda host: token,
            sleep=sleeps.append,
        )
        return CliRunner().invoke(command, args, input=input, obj=app, prog_name=_brand.NAME)

    return run
