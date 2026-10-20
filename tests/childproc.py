"""How a test runs the installed `dynfarm` in a child process, on Linux, macOS and Windows alike.

Why this is a module and not two copies in the fixtures that need it (`tests/e2e/conftest.py`,
`tests/test_secret_leaks.py`): both run the real script in a subprocess with a scrubbed environment, and the two things
that differ between operating systems must be right in one place.

- *Where the script is.* `pip install` puts `dynfarm` (`dynfarm.exe` on Windows) in the interpreter's scripts
  directory. In a virtual environment that is the directory of `sys.executable`; in a plain Python on Windows it is the
  sibling `Scripts` directory, which is not. `sysconfig` knows both, so it is asked as well.
- *What the child needs from the system.* A scrubbed environment is what makes a test independent of a developer's
  shell, but Windows cannot start a Python without `SYSTEMROOT` (sockets and `random` fail with `WinError 10106`
  otherwise), and wants `TEMP` where POSIX wants `TMPDIR`.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path

import pytest

from dynfarm import _brand

#: Variables that name the operating system's own places, not the person's. A child without them cannot even open a
#: socket on Windows; on POSIX none of them is set, so nothing is copied.
SYSTEM_VARIABLES = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")


def installed_binary() -> str:
    """The `dynfarm` script of the environment running the tests; a missing one fails the test, it does not skip it."""
    directories = dict.fromkeys([str(Path(sys.executable).parent), sysconfig.get_path("scripts")])
    for directory in directories:
        found = shutil.which(_brand.NAME, path=directory)
        if found is not None:
            return found
    pytest.fail(
        f"no `{_brand.NAME}` in {' or '.join(directories)}: run `pip install -e '.[dev]'` in this environment first"
    )


def child_environment(home: Path, config_dir: Path, url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The whole environment of a child: its own home, config directory and temp directory, no keyring, no proxy.

    `CI=1` makes the token store skip the keyring, `PYTHON_KEYRING_BACKEND` is keyring's failing backend in case
    something touched it anyway, and `NO_PROXY` keeps a developer's proxy from swallowing the loopback requests.
    `PYTHONUTF8` makes the child's text files and pipes UTF-8 whatever the machine's code page is.
    """
    temp = home / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "TMPDIR": str(temp),
        "TEMP": str(temp),
        "TMP": str(temp),
        "PYTHONUTF8": "1",
        "CI": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        _brand.env("CONFIG_DIR"): str(config_dir),
        _brand.env("HOST"): url,
    }
    env.update({name: os.environ[name] for name in SYSTEM_VARIABLES if name in os.environ})
    env.update(extra or {})
    return env
