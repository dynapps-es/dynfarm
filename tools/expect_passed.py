#!/usr/bin/env python3
"""Gate for a pytest run piped into it: `pytest ... | python3 tools/expect_passed.py MIN`.

It echoes the output and exits 0 only when pytest reports at least MIN passed tests and no failure or error. A run
that selects nothing, or a file that silently lost its tests, exits 0 from pytest's point of view in some setups; a
gate that cannot fail is not a gate, so the count is checked here.
"""

import re
import sys


def main() -> int:
    minimum = int(sys.argv[1])
    text = sys.stdin.read()
    sys.stdout.write(text)
    passed = re.search(r"(\d+) passed", text)
    failed = re.search(r"(\d+) (failed|errors?)\b", text)
    count = int(passed.group(1)) if passed else 0
    if failed or count < minimum:
        print(f"expect_passed: {count} passed (minimum {minimum}), failures: {bool(failed)}", file=sys.stderr)
        return 1
    print(f"expect_passed: {count} passed (minimum {minimum})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
