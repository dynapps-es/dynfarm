#!/usr/bin/env python3
"""Wait until a release can be installed from PyPI: `wait_for_pypi.py VERSION [--index URL] [--attempts N] [--pause S]`.

The release workflow runs it between `publish` and the installs that prove the release works. PyPI accepts an upload
before every mirror and index page shows it, so an install started the second after `publish` can fail with "no such
version" although nothing is wrong. This asks the *simple index* (the page `pip` and `uv` read, in its JSON form), not
the project's JSON API, because that is what the installers will use, and gives up after a bounded time (by default
10 attempts 30 s apart) instead of hanging a release: a version that never appears is a failure to look at.

Standard library only, so it runs before anything is installed; the same on Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PACKAGE = "dynfarm"
INDEX = "https://pypi.org/simple/"
ATTEMPTS = 10
PAUSE_SECONDS = 30.0
TIMEOUT_SECONDS = 30.0
ACCEPT = "application/vnd.pypi.simple.v1+json"


def lists_version(document: object, package: str, version: str) -> bool:
    """Whether a simple-index document (PEP 691) offers a file of `package` at `version`."""
    if not isinstance(document, dict):
        return False
    if version in (document.get("versions") or []):
        return True
    for entry in document.get("files") or []:
        name = entry.get("filename", "") if isinstance(entry, dict) else ""
        # A wheel is `package-version-tags.whl`, an sdist `package-version.tar.gz`.
        if name.startswith((f"{package}-{version}-", f"{package}-{version}.tar.gz")):
            return True
    return False


def visible(index: str, package: str, version: str) -> bool:
    """One look at the index. A 404 (the project or version is not there yet) and a network error are both "not yet"."""
    if not index.startswith(("https://", "http://")):
        raise ValueError(f"not an http(s) URL: {index!r}")
    request = urllib.request.Request(f"{index.rstrip('/')}/{package}/", headers={"Accept": ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - scheme checked
            document = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
        print(f"wait_for_pypi: {package} {version}: not yet ({exc})", file=sys.stderr)
        return False
    return lists_version(document, package, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the release, without the leading v (0.1.0)")
    parser.add_argument("--package", default=PACKAGE)
    parser.add_argument("--index", default=INDEX, help="the simple index (default: PyPI's)")
    parser.add_argument("--attempts", type=int, default=ATTEMPTS)
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS, help="seconds between attempts")
    args = parser.parse_args(argv)
    version = args.version.removeprefix("v")
    for attempt in range(1, args.attempts + 1):
        if visible(args.index, args.package, version):
            print(f"wait_for_pypi: {args.package} {version} is on the index (attempt {attempt}/{args.attempts})")
            return 0
        if attempt < args.attempts:
            time.sleep(args.pause)
    print(f"wait_for_pypi: {args.package} {version} did not appear after {args.attempts} attempts", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
