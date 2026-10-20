"""The fake farm on a real socket: a `ThreadingHTTPServer` on 127.0.0.1 that hands each request to `FakeFarm.handle`.

Why: `CLAUDE.md` says an endpoint is not tested until something calls it from a real socket. The unit tests use
`httpx.MockTransport`, which skips everything below the HTTP client; the end-to-end suite (`tests/e2e`) runs the
installed `dynfarm` binary in a subprocess against this server, so the proxy-free path, the TLS-free loopback rule
and the real exit codes are exercised as a person would meet them.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .farm import FakeFarm


@contextmanager
def serve(farm: FakeFarm) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            request = httpx.Request(self.command, base + self.path, headers=dict(self.headers.items()), content=body)
            try:
                response = farm.handle(request)
            except Exception as exc:  # a FakeFarmBug is already recorded in farm.bugs; the client sees a 500
                payload = str(exc).encode()
                self.send_response(500)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = response.content
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() not in ("content-length", "connection", "transfer-encoding"):
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = _answer

        def log_message(self, *args: object) -> None:  # silence the default stderr access log
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    base = f"http://127.0.0.1:{server.server_address[1]}"
    previous, farm.base_url = farm.base_url, base
    # A short poll interval: `shutdown()` waits for one, and the end-to-end suite starts a server per test.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        farm.base_url = previous
