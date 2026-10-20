"""The secret given to `dynfarm credential set` goes to one place, proven on the installed binary over a real socket.

Why these tests exist: `credential set` is the command that exists so a secret never passes through an LLM, a shell
history or a log. In-process tests (`test_cmd_credential.py`) cannot see everything a real run touches: the real
stdout and stderr file descriptors, the terminal's echo, the files the process writes, the bytes that cross a socket.
So each test here runs the `dynfarm` entry point that `pip install` put next to this interpreter, in a subprocess,
against the fake farm served on 127.0.0.1 (`FakeFarm.serve()`), with a sentinel secret of its own, and then looks for
the sentinel everywhere it must not be:

- stdout and stderr, also with `--debug`;
- every file under the child's config directory and home directory (the config dir holds a real `config.toml`: the
  child logged in first with `--insecure-storage`, so the check is never over an empty directory);
- every request the farm received, except the body of the one `POST /credentials`, where it must be exactly the
  `value` field;
- the child's argv: `--value-env` and `--value-file` put a name or a path on the command line, never the value.

The child never sees a real keyring or a real token: `CI=1` (the token store does not touch a keyring then),
`PYTHON_KEYRING_BACKEND` set to keyring's failing backend in case anything tried, a fake token, and a scrubbed
environment with its own `HOME`. `NO_PROXY` keeps a developer's proxy from swallowing the loopback requests.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from childproc import child_environment, installed_binary
from fakefarm import ADMIN, ALICE, ApiProblem

CREDENTIALS = "/farm/api/v1/credentials"
TIMEOUT = 30


def sentinel() -> str:
    """A secret of this test only, of URL-safe characters so a leak into a URL is found as it is."""
    return f"sk-sentinel-{uuid.uuid4().hex}"


@dataclass
class Run:
    argv: list[str]
    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


class Child:
    """Runs the installed `dynfarm` against one fake farm on a real socket, logged in with a file-stored token."""

    def __init__(self, farm, url: str, root: Path, config_dir: Path):
        self.farm, self.url = farm, url
        self.home, self.config_dir = root / "home", config_dir
        self.home.mkdir()
        self.binary = installed_binary()
        self.runs: list[Run] = []

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return child_environment(self.home, self.config_dir, self.url, extra)

    def run(self, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> Run:
        argv = [self.binary, *args]
        done = subprocess.run(
            argv,
            input=(input or "").encode(),
            capture_output=True,
            env=self.env(env),
            timeout=TIMEOUT,
            check=False,
        )
        run = Run(argv, done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))
        self.runs.append(run)
        return run

    def login(self, token: str = ALICE) -> None:
        run = self.run(
            "auth", "login", "--hostname", self.url, "--with-token", "--insecure-storage", input=token + "\n"
        )
        assert run.code == 0, run.output
        assert (self.config_dir / "config.toml").is_file()


@pytest.fixture
def child(fake_farm, tmp_path, config_dir):
    with fake_farm.serve() as url:
        runner = Child(fake_farm, url, tmp_path, config_dir)
        runner.login()
        fake_farm.requests.clear()
        yield runner


def assert_contained(child: Child, secret: str, *, posts: int = 1) -> None:
    """The whole leak check: the secret is in the body of `posts` POST /credentials and nowhere else at all."""
    for run in child.runs:
        assert secret not in run.stdout, f"stdout of {run.argv[1:]}"
        assert secret not in run.stderr, f"stderr of {run.argv[1:]}"
        assert secret not in " ".join(run.argv), f"argv of {run.argv[1:]}"
    for directory in (child.config_dir, child.home):
        for path in directory.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes(), f"the secret was written to {path}"
    carrying = [r for r in child.farm.requests if secret.encode() in r.text.encode() or secret.encode() in r.body]
    assert [(r.method, r.path) for r in carrying] == [("POST", CREDENTIALS)] * posts
    for request in carrying:
        assert secret not in request.target
        assert secret not in json.dumps(request.headers)
        body = json.loads(request.body)
        assert body.pop("value") == secret
        assert secret not in json.dumps(body)


def stored(fake_farm, name: str, *, scope: str = "personal", subject: str | None = "alice") -> str | None:
    return fake_farm.credential_values.get(("tenant-a", scope, subject if scope == "personal" else None, name))


# -- each way in --------------------------------------------------------------------------------------------------


def test_a_secret_piped_into_standard_input_reaches_only_the_body_of_the_post_even_with_debug(child, fake_farm):
    secret = sentinel()
    run = child.run("--debug", "credential", "set", "git-readonly", "--type", "git_token", input=secret + "\n")
    assert run.code == 0, run.output
    assert run.stdout.startswith("Saved credential git-readonly (git_token, fingerprint sha256:")
    assert "[debug] POST /farm/api/v1/credentials (write_credential) -> 201" in run.stderr
    assert stored(fake_farm, "git-readonly") == secret
    assert_contained(child, secret)


def test_a_secret_in_a_value_file_reaches_only_the_body_of_the_post_and_argv_holds_only_the_path(
    child, fake_farm, tmp_path
):
    secret = sentinel()
    secret_file = tmp_path / "secret.txt"
    secret_file.write_bytes(secret.encode() + b"\n")
    run = child.run(
        "--debug", "credential", "set", "git-readonly", "--type", "git_token", "--value-file", str(secret_file)
    )
    assert run.code == 0, run.output
    assert str(secret_file) in run.argv
    assert stored(fake_farm, "git-readonly") == secret
    assert_contained(child, secret)


def test_value_file_dash_reads_standard_input_and_reaches_only_the_body_of_the_post(child, fake_farm):
    secret = sentinel()
    run = child.run("credential", "set", "git-readonly", "--type", "git_token", "--value-file", "-", input=secret)
    assert run.code == 0, run.output
    assert stored(fake_farm, "git-readonly") == secret
    assert_contained(child, secret)


def test_a_secret_in_an_environment_variable_reaches_only_the_body_of_the_post_and_argv_holds_only_its_name(
    child, fake_farm
):
    secret = sentinel()
    run = child.run(
        "--debug",
        "credential",
        "set",
        "git-readonly",
        "--type",
        "git_token",
        "--value-env",
        "GITLAB_READONLY_TOKEN",
        env={"GITLAB_READONLY_TOKEN": secret},
    )
    assert run.code == 0, run.output
    assert "GITLAB_READONLY_TOKEN" in run.argv
    assert stored(fake_farm, "git-readonly") == secret
    assert_contained(child, secret)


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are POSIX; Windows has no pty to drive")
def test_a_secret_typed_at_the_hidden_prompt_is_never_echoed_by_the_terminal(child, fake_farm):
    """On a real pseudo-terminal, the terminal driver echoes whatever is typed unless the prompt turned echo off: the
    transcript of everything the terminal showed must not hold the secret."""
    import pty

    secret = sentinel()
    argv = [child.binary, "--debug", "credential", "set", "git-readonly", "--type", "git_token"]
    master, slave = pty.openpty()
    # A session of its own: the child has no controlling terminal, so getpass cannot open the developer's real one
    # (/dev/tty) and uses this pseudo-terminal, which is its standard input.
    process = subprocess.Popen(
        argv, stdin=slave, stdout=slave, stderr=slave, env=child.env(), start_new_session=True, close_fds=True
    )
    os.close(slave)
    shown = b""

    def read_until(marker: bytes | None) -> None:
        nonlocal shown
        deadline = time.monotonic() + TIMEOUT
        while marker is None or marker not in shown:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {marker!r}; the terminal showed {shown!r}"
            ready, _, _ = select.select([master], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the child closed the terminal
                chunk = b""
            if not chunk:
                assert marker is None, f"the child exited before {marker!r}; the terminal showed {shown!r}"
                return
            shown += chunk

    try:
        # The prompt text is written after echo is turned off, so answering only once it is shown is safe.
        read_until(b"Secret value (hidden): ")
        os.write(master, secret.encode() + b"\n")
        read_until(b"Repeat it: ")
        os.write(master, secret.encode() + b"\n")
        read_until(None)
        code = process.wait(timeout=TIMEOUT)
    finally:
        if process.poll() is None:
            process.kill()
        os.close(master)
    child.runs.append(Run(argv, code, shown.decode("utf-8", "replace"), ""))
    assert code == 0, shown
    assert b"Saved credential git-readonly" in shown and b"[debug] POST /farm/api/v1/credentials" in shown
    assert stored(fake_farm, "git-readonly") == secret
    assert_contained(child, secret)


# -- refusals and failures ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "form",
    [["--value", "{s}"], ["--value={s}"], ["{s}"]],
    ids=["value-space", "value-equals", "positional"],
)
def test_a_secret_given_in_argv_is_refused_with_exit_2_without_being_repeated_or_sent(child, fake_farm, form):
    secret = sentinel()
    args = [part.format(s=secret) for part in form]
    run = child.run("--debug", "credential", "set", "git-readonly", "--type", "git_token", *args)
    assert run.code == 2
    assert secret not in run.stdout and secret not in run.stderr
    assert "--value-file FILE or --value-env VARIABLE" in run.stderr
    assert fake_farm.requests == []


def test_a_value_the_farm_rejects_is_not_echoed_back(child, fake_farm):
    secret = "http://" + sentinel() + ".example.com/mcp"
    run = child.run("--debug", "credential", "set", "docs-mcp", "--type", "mcp_url", input=secret + "\n")
    assert run.code == 1 and "(credential_value_invalid)" in run.stderr
    assert_contained(child, secret)


def test_a_farm_error_that_quotes_the_value_is_masked_before_it_is_shown(child, fake_farm):
    secret = sentinel()

    def echoing(call):
        raise ApiProblem(
            422, "credential_value_invalid", f"rejected {call.body['value']}", details={"got": call.body["value"]}
        )

    fake_farm.override("write_credential", echoing)
    run = child.run("--debug", "credential", "set", "git-readonly", "--type", "git_token", input=secret + "\n")
    assert run.code == 1 and "rejected [redacted] (credential_value_invalid)" in run.stderr
    assert_contained(child, secret)


def test_a_failed_write_is_sent_once_and_never_repeated_by_itself(child, fake_farm):
    secret = sentinel()
    fake_farm.fail_next("write_credential", status=503, code="secret_store_unavailable", retry_after=1)
    run = child.run("--debug", "credential", "set", "git-readonly", "--type", "git_token", input=secret + "\n")
    assert run.code == 1 and "(secret_store_unavailable)" in run.stderr
    assert_contained(child, secret, posts=1)


# -- the life of a credential -------------------------------------------------------------------------------------


def test_rotating_listing_viewing_and_deleting_never_show_either_value(child, fake_farm):
    first, second = sentinel(), sentinel()
    runs = [
        child.run("--debug", "credential", "set", "git-readonly", "--type", "git_token", input=first + "\n"),
        child.run("--debug", "credential", "set", "git-readonly", "--type", "git_token", "--json", input=second),
        child.run("--debug", "credential", "list"),
        child.run("credential", "list", "--json"),
        child.run("--debug", "credential", "view", "git-readonly"),
        child.run("credential", "view", "git-readonly", "--json"),
        child.run("--debug", "credential", "delete", "git-readonly", "--yes"),
    ]
    assert [run.code for run in runs] == [0] * len(runs), [run.output for run in runs]
    assert json.loads(runs[1].stdout)["fingerprint"].startswith("sha256:")
    assert "git-readonly" in runs[2].stdout and "deleted credential git-readonly" in runs[6].stdout
    assert stored(fake_farm, "git-readonly") is None
    assert_contained(child, first)
    assert_contained(child, second)


def test_a_shared_credential_written_by_an_admin_is_contained_too(child, fake_farm):
    child.login(ADMIN)
    fake_farm.requests.clear()
    secret = sentinel()
    run = child.run("--debug", "credential", "set", "web-api", "--type", "generic", "--scope", "shared", input=secret)
    assert run.code == 0, run.output
    assert run.stdout.startswith("Saved shared credential web-api (generic, fingerprint sha256:")
    assert stored(fake_farm, "web-api", scope="shared") == secret
    assert_contained(child, secret)
