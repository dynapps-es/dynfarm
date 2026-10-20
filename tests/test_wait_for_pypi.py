"""`tools/wait_for_pypi.py` holds the release's installs until PyPI shows the version, and gives up in bounded time.

Why this test exists: the smoke installs after `publish` are the only proof that a published release works on every
system. They must not fail because an index page is a few seconds behind an upload, nor wait forever for a version
that will never appear. The "PyPI" is a throw-away HTTP server on 127.0.0.1 that answers in turn, in the JSON form of
the simple index; `NO_PROXY` keeps a developer's proxy from swallowing the requests.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("wait_for_pypi", ROOT / "tools" / "wait_for_pypi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def index_with(*filenames: str, versions: list[str] | None = None) -> tuple[int, bytes]:
    document = {"name": "dynfarm", "files": [{"filename": name} for name in filenames]}
    if versions is not None:
        document["versions"] = versions
    return 200, json.dumps(document).encode()


@contextmanager
def serving(*answers: tuple[int, bytes]) -> Iterator[tuple[str, list[dict[str, str]]]]:
    """A server that gives these answers in turn (the last one repeats); yields its URL and what it was asked."""
    hits: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
            answer = answers[min(len(hits), len(answers) - 1)]
            hits.append({"path": self.path, "accept": self.headers.get("Accept", "")})
            self.send_response(answer[0])
            self.send_header("Content-Length", str(len(answer[1])))
            self.end_headers()
            self.wfile.write(answer[1])

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/simple/", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait(tool, url: str, *extra: str, version: str = "0.1.0") -> int:
    return tool.main([version, "--index", url, "--pause", "0", *extra])


def test_a_version_that_is_already_listed_is_found_at_once_by_asking_for_the_json_form(tool):
    with serving(index_with("dynfarm-0.1.0-py3-none-any.whl", "dynfarm-0.1.0.tar.gz")) as (url, hits):
        assert wait(tool, url) == 0
    assert hits == [{"path": "/simple/dynfarm/", "accept": "application/vnd.pypi.simple.v1+json"}]


def test_a_version_that_appears_after_a_few_attempts_is_waited_for(tool):
    not_yet = (404, b"not found")
    older = index_with("dynfarm-0.0.9-py3-none-any.whl")
    with serving(not_yet, older, index_with("dynfarm-0.1.0.tar.gz")) as (url, hits):
        assert wait(tool, url) == 0
    assert len(hits) == 3


def test_a_version_that_never_appears_ends_after_the_attempts_it_was_given_and_says_so(tool, capsys):
    with serving(index_with("dynfarm-0.0.9-py3-none-any.whl")) as (url, hits):
        assert wait(tool, url, "--attempts", "4") == 1
    assert len(hits) == 4
    assert "did not appear after 4 attempts" in capsys.readouterr().err


def test_a_version_that_only_starts_like_the_one_asked_for_does_not_count(tool):
    """`0.1.0` must not be found in the files of `0.1.0.post1`, of `0.1.01` or of another package that begins alike."""
    for name in ("dynfarm-0.1.0.post1-py3-none-any.whl", "dynfarm-0.1.01.tar.gz", "dynfarm_extra-0.1.0.tar.gz"):
        assert tool.lists_version({"files": [{"filename": name}]}, "dynfarm", "0.1.0") is False, name
    assert tool.lists_version({"files": [{"filename": "dynfarm-0.1.0.tar.gz"}]}, "dynfarm", "0.1.0") is True
    assert tool.lists_version({"versions": ["0.1.0"], "files": []}, "dynfarm", "0.1.0") is True


@pytest.mark.parametrize("document", [None, [], "dynfarm-0.1.0", {"files": "x"}, {"files": [3, None, {}]}])
def test_an_answer_of_another_shape_is_not_a_listing(tool, document):
    assert tool.lists_version(document, "dynfarm", "0.1.0") is False


def test_a_server_that_answers_something_that_is_not_json_is_waited_for_not_crashed_on(tool, capsys):
    with serving((200, b"<html>maintenance</html>")) as (url, hits):
        assert wait(tool, url, "--attempts", "2") == 1
    assert len(hits) == 2 and "not yet" in capsys.readouterr().err


def test_the_leading_v_of_a_tag_is_accepted(tool):
    with serving(index_with("dynfarm-0.1.0.tar.gz")) as (url, _):
        assert wait(tool, url, version="v0.1.0") == 0


def test_only_http_and_https_indexes_are_asked(tool):
    with pytest.raises(ValueError, match="not an http"):
        tool.visible("file:///etc/passwd", "dynfarm", "0.1.0")
