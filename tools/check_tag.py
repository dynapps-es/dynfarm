#!/usr/bin/env python3
"""Gate of the release: `python3 tools/check_tag.py v0.1.0 [--notes release-notes.md]`.

The GitHub release workflow runs it first, and the GitLab pipeline runs it on a tag too.

A tag `v<version>` may only release the version the code says it is. The version has one source, `__version__` in
`src/dynfarm/__init__.py`; the wheel is built from it, so a tag cut on the wrong commit, or before the bump, would
publish a package whose version disagrees with its tag, and PyPI never lets a version be uploaded twice.
This script fails the pipeline before anything is published when:

- the tag is not exactly `v` + the package version;
- `CHANGELOG.md` has no `## [<version>]` section, because the GitHub Release takes its notes from it.

With `--notes FILE` it also writes that section (without its heading) to FILE for the release job.

It reads the version with a regular expression instead of importing the package, so it runs on a bare checkout, before
anything is installed. Exit codes: 0 ok, 1 the tag or the changelog is wrong, 2 usage.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "dynfarm" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"

_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']\s*$""", re.MULTILINE)


def package_version(path: Path = VERSION_FILE) -> str:
    found = _VERSION.search(path.read_text(encoding="utf-8"))
    if not found:
        raise ValueError(f'no `__version__ = "..."` line in {path}')
    return found.group(1)


def changelog_section(version: str, text: str) -> str | None:
    """The body of `## [<version>]` up to the next `## ` heading, or None when there is no such section."""
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if line.startswith(f"## [{version}]"):
            body: list[str] = []
            for following in lines[number + 1 :]:
                if following.startswith("## "):
                    break
                body.append(following)
            return "\n".join(body).strip()
    return None


def check(tag: str, version: str, changelog_text: str) -> list[str]:
    """What is wrong with releasing `tag` when the package says `version`; empty when nothing is."""
    problems: list[str] = []
    if tag != f"v{version}":
        problems.append(f"the tag is {tag!r} but the package version is {version!r}: expected the tag 'v{version}'")
    section = changelog_section(version, changelog_text)
    if section is None:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section for the release notes")
    elif not section:
        problems.append(f"the '## [{version}]' section of CHANGELOG.md is empty")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that a release tag matches the package version.")
    parser.add_argument("tag", help="the tag being released, e.g. v0.1.0 ($CI_COMMIT_TAG, $GITHUB_REF_NAME)")
    parser.add_argument("--notes", type=Path, help="write the changelog section of this version to FILE")
    args = parser.parse_args(argv)

    try:
        version = package_version()
        changelog_text = CHANGELOG.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"check_tag: {error}", file=sys.stderr)
        return 1

    problems = check(args.tag, version, changelog_text)
    for problem in problems:
        print(f"check_tag: {problem}", file=sys.stderr)
    if problems:
        return 1
    if args.notes:
        args.notes.write_text((changelog_section(version, changelog_text) or "") + "\n", encoding="utf-8")
    print(f"check_tag: {args.tag} releases version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
