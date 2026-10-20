"""`tools/fetch_openapi.py` brings the farm's OpenAPI to the drift test, and refreshes the pinned fixture exactly.

Why this test exists: the scheduled `api-drift` job and the fixture refresh in `CONTRIBUTING.md` both rely on this
script. If it wrote a wrong document, or half of one, the drift job would compare the CLI with garbage (and go red for
a reason that is not the API's), and a refresh would produce a noisy diff. The farm is a throw-away HTTP server on
127.0.0.1 serving the pinned document; `NO_PROXY` keeps a developer's proxy from swallowing the requests.
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
FIXTURE = ROOT / "tests" / "fixtures" / "openapi.json"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("fetch_openapi", ROOT / "tools" / "fetch_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_proxy_and_no_waiting(monkeypatch, tool):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(tool, "PAUSE_SECONDS", 0.0)


@contextmanager
def serving(*answers: tuple[int, bytes]) -> Iterator[tuple[str, list[str]]]:
    """A server that gives these answers in turn (the last one repeats); yields its URL and the paths it was asked."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
            answer = answers[min(len(hits), len(answers) - 1)]
            hits.append(self.path)
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
        yield f"http://127.0.0.1:{server.server_address[1]}/openapi.json", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_pretty_download_of_the_pinned_document_is_byte_identical_to_the_fixture(tool, tmp_path):
    compact = json.dumps(json.loads(FIXTURE.read_text())).encode()
    destination = tmp_path / "openapi.json"
    with serving((200, compact)) as (url, _):
        assert tool.main([url, str(destination), "--pretty"]) == 0
    assert destination.read_bytes() == FIXTURE.read_bytes()


def test_a_download_without_pretty_keeps_the_document_as_the_farm_sent_it(tool, tmp_path):
    destination = tmp_path / "live.json"
    with serving((200, FIXTURE.read_bytes())) as (url, _):
        assert tool.main([url, str(destination)]) == 0
    assert json.loads(destination.read_text()) == json.loads(FIXTURE.read_text())


@pytest.mark.parametrize(
    "body",
    [b"<html>maintenance</html>", b'["not", "an", "object"]', b'{"openapi": "3.1.0"}', b'{"paths": {}}'],
    ids=["html", "array", "no-paths", "no-openapi"],
)
def test_an_answer_that_is_not_an_openapi_document_is_refused_and_nothing_is_written(tool, tmp_path, body, capsys):
    destination = tmp_path / "live.json"
    with serving((200, body)) as (url, _):
        assert tool.main([url, str(destination)]) == 1
    assert not destination.exists()
    assert "fetch_openapi:" in capsys.readouterr().err


def test_a_farm_that_answers_an_error_a_couple_of_times_is_retried(tool, tmp_path):
    destination = tmp_path / "live.json"
    with serving((502, b"bad gateway"), (503, b"unavailable"), (200, FIXTURE.read_bytes())) as (
        url,
        hits,
    ):
        assert tool.main([url, str(destination)]) == 0
    assert len(hits) == 3
    assert destination.exists()


def test_a_farm_that_keeps_failing_ends_the_job_after_three_attempts_with_nothing_written(tool, tmp_path, capsys):
    destination = tmp_path / "live.json"
    with serving((502, b"bad gateway")) as (url, hits):
        assert tool.main([url, str(destination)]) == 1
    assert len(hits) == tool.ATTEMPTS
    assert not destination.exists()
    assert "cannot download" in capsys.readouterr().err


def test_only_http_and_https_addresses_are_fetched(tool, tmp_path, capsys):
    assert tool.main(["file:///etc/passwd", str(tmp_path / "x.json")]) == 1
    assert not (tmp_path / "x.json").exists()
    assert "not an http(s) URL" in capsys.readouterr().err
