"""Fixtures of the end-to-end suite: the installed `dynfarm` binary, a fake farm on a real socket, nothing shared.

Why a suite of its own: the unit tests run the command tree in-process over `httpx.MockTransport`. They cannot see a
broken entry point, a module missing from the wheel, an exit code that never reaches the shell, a proxy variable that
swallows the request, a token that leaks only through the real file descriptors, or the way a command behaves when it
is not on a terminal. Here every command is the real `dynfarm` script that `pip install` put next to this interpreter
(the CI job installs the package non-editable first, so it is the built package that runs), in a subprocess, against
`FakeFarm.serve()` on 127.0.0.1.

The child gets a scrubbed environment so a developer's machine cannot change the result: its own `HOME`, config
directory and temp directory, `CI=1` (the token store then never touches a keyring, so nothing asks the OS for a
password), `PYTHON_KEYRING_BACKEND` set to keyring's failing backend in case something did, and `NO_PROXY` so a proxy
in the developer's shell does not swallow the loopback requests. Tokens are the fake farm's fixed fake ones.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from childproc import child_environment, installed_binary
from fakefarm import ALICE, FakeFarm

TIMEOUT = 60


@dataclass
class Run:
    argv: list[str]
    code: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def json(self) -> Any:
        return json.loads(self.stdout)


@dataclass
class Installed:
    """The installed binary, aimed at one fake farm, with its own home and config directory."""

    url: str
    home: Path
    config_dir: Path
    binary: str = field(default_factory=installed_binary)
    runs: list[Run] = field(default_factory=list)

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return child_environment(self.home, self.config_dir, self.url, extra)

    def run(self, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> Run:
        """Run `dynfarm ARGS`; the standard input is `input` (a closed, empty pipe by default: not a terminal)."""
        argv = [self.binary, *args]
        started = time.monotonic()
        done = subprocess.run(
            argv,
            input=(input or "").encode(),
            capture_output=True,
            env=self.env(env),
            timeout=TIMEOUT,
            check=False,
        )
        run = Run(
            argv,
            done.returncode,
            done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"),
            time.monotonic() - started,
        )
        self.runs.append(run)
        return run

    def login(self, token: str = ALICE) -> Run:
        """Log in as in a CI job: the token on standard input, kept in the config file (there is no keyring)."""
        return self.run(
            "auth", "login", "--hostname", self.url, "--with-token", "--insecure-storage", input=token + "\n"
        )


@pytest.fixture
def dynfarm(fake_farm: FakeFarm, tmp_path: Path) -> Iterator[Installed]:
    """The installed binary against a fake farm on a real socket; not logged in."""
    with fake_farm.serve() as url:
        yield Installed(url=url, home=tmp_path / "home", config_dir=tmp_path / "config-e2e")


@pytest.fixture
def dynfarm_bin(dynfarm: Installed) -> Installed:
    """The binary for tests that do not care about the farm (`--version`, `--help`)."""
    return dynfarm


@pytest.fixture
def logged_in(dynfarm: Installed, fake_farm: FakeFarm) -> Installed:
    """The same, logged in as `alice` (the requests of the login are forgotten: tests assert on what comes after)."""
    run = dynfarm.login()
    assert run.code == 0, run.output
    fake_farm.requests.clear()
    return dynfarm


@pytest.fixture
def finish_after_creation(fake_farm: FakeFarm) -> Callable[..., None]:
    """`finish_after_creation({"state": "running"}, {"state": "succeeded"})`: walk the next launched task through them.

    One step per `GET /tasks/{id}` (fields of `FakeFarm.set_task`): what watching a real task is like. The farm answers
    `queued` at creation and the state moves while the task is watched.
    """

    def arrange(*steps: dict[str, Any]) -> None:
        queue = list(steps)
        holder: dict[str, Callable[..., tuple[int, Any]]] = {}
        created: list[str] = []

        def create(call):
            status, body = holder["create"](call)
            created.append(body["task_id"])
            return status, body

        def get(call):
            if queue and created:
                fake_farm.set_task(created[-1], **queue.pop(0))
            return holder["get"](call)

        holder["create"] = fake_farm.override("create_task", create)
        holder["get"] = fake_farm.override("get_task", get)

    return arrange
