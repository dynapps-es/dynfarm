"""Fixtures shared by every test: a clean environment, an isolated config dir, the fake farm and a CLI runner.

Why the first two are `autouse`: a test that forgot to isolate would read the developer's real `DYNFARM_TOKEN` or
write into their real config directory. Isolation is the default; a test has to go out of its way to leave it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result
from fakefarm import ALICE, FakeFarm

from dynfarm import _brand
from dynfarm.cli import cli
from dynfarm.context import AppContext


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith((_brand.ENV_PREFIX, "AGENT_FARM_")) or name in ("NO_COLOR", "TERM"):
            monkeypatch.delenv(name)


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
