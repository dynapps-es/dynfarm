"""`dynfarm` behaves the same on Linux, macOS and Windows, and the tests that say so can fail on any of them.

Why these tests exist: the development pipeline runs on Linux only, and the three operating systems differ in ways a
reviewer does not see in a diff. Each test pins one difference where it is cheap, on every machine:

- `python -m dynfarm` is the entry point that works when the scripts directory of a fresh Windows Python is not on
  `PATH`, and it keeps the program name `dynfarm`;
- a text file is read and written with an explicit encoding (a Windows default is a legacy code page, so a message with
  a dash in it, or a prompt with an accent, would change between machines), and the scan that says so can find a call
  that forgets;
- the config file is saved without calling `os.chmod` or `os.fchmod`, which a Windows Python does not have;
- no POSIX-only module is imported by the package;
- what the farm sends is printable on a standard output whose encoding is narrow (a Windows pipe), and a prompt file
  saved with a byte-order mark by a Windows editor is read without it.
"""

from __future__ import annotations

import ast
import io
import os
import runpy
import subprocess
import sys
import types
from pathlib import Path

import pytest

import dynfarm
from dynfarm import cli, config, inputs

ROOT = Path(__file__).resolve().parents[1]

# -- python -m dynfarm -----------------------------------------------------------------------------------------------


def test_python_dash_m_dynfarm_prints_the_version_under_the_name_of_the_command():
    run = subprocess.run(
        [sys.executable, "-m", "dynfarm", "--version"], capture_output=True, text=True, check=False, timeout=60
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == f"dynfarm {dynfarm.__version__}"


def test_python_dash_m_dynfarm_shows_dynfarm_and_not_the_module_name_in_its_usage():
    run = subprocess.run(
        [sys.executable, "-m", "dynfarm", "task", "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert run.returncode == 0, run.stderr
    assert "Usage: dynfarm task" in run.stdout and "__main__" not in run.stdout


def test_the_module_entry_point_runs_the_same_main_as_the_script(monkeypatch):
    """In-process, so that the lines of `__main__.py` are measured; the subprocess tests above prove the real thing."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8", write_through=True))
    monkeypatch.setattr(sys, "argv", ["dynfarm", "--version"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("dynfarm", run_name="__main__", alter_sys=False)
    assert stopped.value.code == 0
    assert raw.getvalue().decode().strip() == f"dynfarm {dynfarm.__version__}"


# -- a standard output that cannot hold every character --------------------------------------------------------------


def legacy_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", write_through=True)


def test_a_legacy_code_page_standard_output_would_stop_on_a_name_from_the_farm_without_the_safeguard():
    """The control: this is the crash the safeguard exists for, so the next test proves something."""
    _, stream = legacy_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write("task 日本")


def test_a_character_the_standard_output_lacks_is_written_as_an_escape_not_as_a_traceback(monkeypatch):
    raw, stream = legacy_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    cli._survive_a_narrow_terminal()
    print("task 日本 ok")
    assert raw.getvalue() == b"task \\u65e5\\u672c ok\n"


def test_a_standard_output_that_cannot_be_reconfigured_is_left_alone(monkeypatch):
    """pythonw.exe has no standard output at all (`None`), and an embedding may put any object there."""
    for replacement in (None, object()):
        monkeypatch.setattr(sys, "stdout", replacement)
        cli._survive_a_narrow_terminal()


# -- a file saved by a Windows editor --------------------------------------------------------------------------------


def test_a_prompt_file_saved_with_a_byte_order_mark_is_read_without_it(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes(b"\xef\xbb\xbfReview the change.\r\n")
    assert inputs.read_text(str(prompt), "--prompt-file") == "Review the change."


def test_a_json_file_saved_with_a_byte_order_mark_is_still_json(tmp_path):
    spec = tmp_path / "task.json"
    spec.write_bytes(b'\xef\xbb\xbf{"prompt": "hi"}\r\n')
    assert inputs.read_json_object(str(spec), "--from-file", "a task") == {"prompt": "hi"}


# -- the config file on a system without POSIX permissions -----------------------------------------------------------


class WindowsOs:
    """`os` as a Windows Python has it: `name` is `nt`, and `chmod` of a mode and `fchmod` do not exist.

    `locked_for` is how many times `os.replace` fails with `PermissionError` before it works: a destination that a
    virus scanner or the indexer has open for a moment.
    """

    name = "nt"

    def __init__(self, locked_for: int = 0, name: str = "nt") -> None:
        self.name = name
        self.locked_for = locked_for
        self.replace_calls = 0

    def replace(self, source, target):
        self.replace_calls += 1
        if self.replace_calls <= self.locked_for:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return os.replace(source, target)

    def __getattr__(self, attribute: str):
        if attribute in ("fchmod", "chmod") and self.name == "nt":
            raise AttributeError(f"module 'os' has no attribute {attribute!r} on Windows")
        return getattr(os, attribute)


def test_the_config_file_is_saved_and_read_back_where_there_is_no_fchmod(monkeypatch, config_dir):
    monkeypatch.setattr(config, "os", WindowsOs())
    data = {"default_host": "https://a.example.com", "hosts": {"https://a.example.com": {"token": "tok-plain"}}}
    path = config.save_config(data)
    assert path == config_dir / "config.toml"
    assert config.load_config() == data
    assert [entry.name for entry in config_dir.iterdir()] == ["config.toml"]  # and no temporary file left behind


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    """The pauses the config module asked for; none is really waited."""
    asked: list[float] = []
    monkeypatch.setattr(config, "time", types.SimpleNamespace(sleep=asked.append))
    return asked


def test_a_config_file_that_a_scanner_holds_open_for_a_moment_is_still_saved_on_windows(
    monkeypatch, config_dir, pauses
):
    windows = WindowsOs(locked_for=2)
    monkeypatch.setattr(config, "os", windows)
    path = config.save_config({"default_host": "https://a.example.com"})
    assert windows.replace_calls == 3 and len(pauses) == 2
    assert config.load_config()["default_host"] == "https://a.example.com" and path.is_file()


def test_a_file_that_stays_locked_is_an_error_after_a_few_tries_and_leaves_no_temporary_file(
    monkeypatch, config_dir, pauses
):
    config.save_config({"default_host": "https://old.example.com"})
    windows = WindowsOs(locked_for=99)
    monkeypatch.setattr(config, "os", windows)
    with pytest.raises(PermissionError):
        config.save_config({"default_host": "https://new.example.com"})
    assert windows.replace_calls == 5 and len(pauses) == 4
    assert config.load_config()["default_host"] == "https://old.example.com"
    assert [entry.name for entry in config_dir.iterdir()] == ["config.toml"]


def test_a_permission_error_is_reported_at_once_where_there_is_no_such_window(monkeypatch, config_dir, pauses):
    posix = WindowsOs(locked_for=99, name="posix")
    monkeypatch.setattr(config, "os", posix)
    with pytest.raises(PermissionError):
        config.save_config({"default_host": "https://a.example.com"})
    assert posix.replace_calls == 1 and pauses == []


# -- what the package imports and how it reads files -----------------------------------------------------------------

#: Modules only POSIX has: importing one at the top of a module is an ImportError for every command on Windows.
POSIX_ONLY_MODULES = {"fcntl", "termios", "pwd", "grp", "resource", "pty", "tty", "syslog"}


def imported_modules(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.partition(".")[0])
    return found


def test_the_package_imports_no_module_that_only_posix_has():
    offenders = {
        str(path.relative_to(ROOT)): sorted(imported_modules(path.read_text(encoding="utf-8")) & POSIX_ONLY_MODULES)
        for path in (ROOT / "src").rglob("*.py")
    }
    assert {name: modules for name, modules in offenders.items() if modules} == {}


def test_the_posix_import_scan_can_fail():
    assert imported_modules("import fcntl\nfrom termios import tcgetattr\nimport os.path") >= {"fcntl", "termios", "os"}
    assert imported_modules("def f():\n    import pty\n") == {"pty"}  # an import inside a function is found too


def calls_without_encoding(source: str) -> list[int]:
    """The line numbers of `read_text()` / `write_text(x)` / text-mode `open()` calls that do not say their encoding."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        says = any(keyword.arg == "encoding" for keyword in node.keywords)
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("read_text", "write_text"):
            positional = 1 if node.func.attr == "read_text" else 2  # (encoding) / (data, encoding)
            if not says and len(node.args) < positional:
                lines.append(node.lineno)
        elif isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = (
                node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
            )
            binary = isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value
            if not says and not binary and len(node.args) < 4:
                lines.append(node.lineno)
    return sorted(lines)


def test_every_text_file_the_repository_reads_or_writes_names_its_encoding():
    """A Windows machine's default is a legacy code page, and its default line ending is `\\r\\n`."""
    offenders = {}
    for folder in ("src", "tests", "tools"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if lines := calls_without_encoding(path.read_text(encoding="utf-8")):
                offenders[str(path.relative_to(ROOT))] = lines
    assert offenders == {}, "pass encoding=... (and newline='' or bytes where the exact bytes matter)"


def test_the_encoding_scan_can_fail_and_does_not_flag_what_is_fine():
    flagged = "p.read_text()\np.write_text('x')\nopen('f')\nopen('f', 'r')\nopen(path, mode='w')\n"
    assert calls_without_encoding(flagged) == [1, 2, 3, 4, 5]
    fine = (
        "p.read_text(encoding='utf-8')\np.write_text('x', encoding='utf-8')\np.read_text('utf-8')\n"
        "open('f', 'rb')\nopen('f', 'w', encoding='utf-8')\nopen(path, mode='wb')\ninputs.read_text(source, flag)\n"
    )
    assert calls_without_encoding(fine) == []


# -- line endings ----------------------------------------------------------------------------------------------------


def test_text_files_are_checked_out_with_lf_on_every_system():
    """A Windows checkout would otherwise write `\\r\\n` into `install.sh` (an invalid shell script) and into the pinned
    OpenAPI document (which a test compares byte for byte with what `tools/fetch_openapi.py` writes)."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "* text=auto eol=lf" in attributes


def test_the_files_whose_bytes_matter_have_no_carriage_return_in_this_checkout():
    for name in ("install.sh", "tests/fixtures/openapi.json", "tools/check_tag.py", "src/dynfarm/cli.py"):
        assert b"\r" not in (ROOT / name).read_bytes(), f"{name} has CRLF line endings: is .gitattributes applied?"
