#!/usr/bin/env python3
"""Download an Agent Farm OpenAPI document and check it is one: `fetch_openapi.py URL DEST [--pretty]`.

Two callers, one recipe. The scheduled `api-drift` CI job saves the live document of a farm (the `/openapi.json` of a
farm needs no token) so `tests/test_parity.py` can compare it with the CLI. A contributor refreshes the pinned
`tests/fixtures/openapi.json` with `--pretty`, which writes it indented with sorted keys and a final newline, the way
the file is committed, so a refresh shows up in a diff as the API's change and nothing else.

Standard library only, so it runs before anything is installed. It retries a few times (a farm being rolled out answers
a 502 for a moment), refuses anything that is not a JSON object with `openapi` and `paths`, and writes the file only
when all that is true: a half-written or wrong document must never reach a test as if it were the farm's.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ATTEMPTS = 3
PAUSE_SECONDS = 5.0
TIMEOUT_SECONDS = 30.0


def fetch(url: str) -> bytes:
    """The body at `url` (http or https only); raises `OSError` after `ATTEMPTS` failures."""
    if not url.startswith(("https://", "http://")):
        raise OSError(f"not an http(s) URL: {url!r}")
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - scheme checked
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            print(f"fetch_openapi: attempt {attempt}/{ATTEMPTS} for {url} failed: {exc}", file=sys.stderr)
            if attempt < ATTEMPTS:
                time.sleep(PAUSE_SECONDS)
    raise OSError(f"cannot download {url}: {last}")


def parse(body: bytes) -> dict:
    """The document in `body`, or `ValueError` saying what is wrong with it."""
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise ValueError(f"the answer is not JSON: {exc}") from exc
    if not isinstance(document, dict) or "openapi" not in document or not isinstance(document.get("paths"), dict):
        raise ValueError("the answer is JSON but not an OpenAPI document (no `openapi` and `paths`)")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("dest", type=Path)
    parser.add_argument("--pretty", action="store_true", help="indent and sort keys, as the pinned fixture is kept")
    args = parser.parse_args(argv)
    try:
        document = parse(fetch(args.url))
    except (OSError, ValueError) as exc:
        print(f"fetch_openapi: {exc}", file=sys.stderr)
        return 1
    if args.pretty:
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    else:
        text = json.dumps(document)
    args.dest.write_text(text)
    operations = sum(1 for item in document["paths"].values() for method in item if method != "parameters")
    print(f"fetch_openapi: saved {args.dest} ({operations} operations, openapi {document['openapi']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
