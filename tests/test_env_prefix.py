"""Every environment variable of the tool carries the `DYNFARM_` prefix, and the code cannot quietly grow another.

Why this test exists: `dynfarm` once read `farm.py`'s `AGENT_FARM_TOKEN` and `AGENT_FARM_URL` as aliases. A token that
somebody exported in a shell profile for one tool then followed them into another, and a name without our prefix is
a name some other tool owns. The rule is now absolute: a variable is `DYNFARM_*`, or it is a standard convention that
is not ours (`CI`, `NO_COLOR`, `HOME`, ...) and is listed below, with the reason, in `ALLOWED`.

The scan reads `src/dynfarm/**/*.py` (through `ast`, so a variable named in a docstring is not a read) and both
install scripts (`$VAR`, `${VAR}`, `$env:VAR`). A scanner that cannot fail proves nothing, so the same functions are
also run on synthetic snippets that read `AGENT_FARM_TOKEN` in every way the code or the scripts could, and must
flag it. The last tests pin the other half of the rule: the removed names appear nowhere a person or a script reads.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from dynfarm import _brand

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "dynfarm"
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"

PYTHON, SH, PS1 = "python", "sh", "ps1"


@dataclass(frozen=True)
class Allowed:
    """A variable that is not ours, where it may appear (`scopes`; none = only a dependency reads it) and why."""

    reason: str
    scopes: tuple[str, ...]


#: The external conventions. Anything else outside `DYNFARM_*` fails `test_every_variable_is_ours_or_a_listed_one`.
ALLOWED: dict[str, Allowed] = {
    "CI": Allowed("set by every CI system; with it the token store never touches a keyring", (PYTHON,)),
    "NO_COLOR": Allowed("the no-color.org convention: no ANSI colour in the output", (PYTHON,)),
    "TERM": Allowed("the terminal's type; `dumb` means no colour", (PYTHON,)),
    "HOME": Allowed("the OS's home directory; where uv's installer puts `uv`", (SH,)),
    "PATH": Allowed(
        "the OS's command search path; the scripts add uv's directory to it for their own checks", (SH, PS1)
    ),
    "XDG_BIN_HOME": Allowed(
        "the freedesktop convention for the per-user bin directory, which uv's installer honours", (SH,)
    ),
    "USERPROFILE": Allowed("Windows' home directory; where uv's installer puts `uv`", (PS1,)),
    "UV_INDEX_DYNFARM_USERNAME": Allowed(
        "uv's own credential variable for the index called `dynfarm`; the scripts only SET it, for the uv process",
        (SH, PS1),
    ),
    "UV_INDEX_DYNFARM_PASSWORD": Allowed(
        "uv's own credential variable for the index called `dynfarm`; the scripts only SET it, for the uv process",
        (SH, PS1),
    ),
    # Read by httpx and by nothing of ours: the tool must leave proxying to the HTTP client, not reimplement it.
    "HTTP_PROXY": Allowed("the proxy convention, honoured by httpx itself", ()),
    "HTTPS_PROXY": Allowed("the proxy convention, honoured by httpx itself", ()),
    "ALL_PROXY": Allowed("the proxy convention, honoured by httpx itself", ()),
    "NO_PROXY": Allowed("the proxy convention, honoured by httpx itself", ()),
}

#: Reads whose variable name is not a literal in the source, so the scan cannot see it: each is a name the PERSON chose.
DYNAMIC_READS: dict[str, str] = {
    "value_env": "`credential set --value-env VARIABLE`: the person names the variable that holds the secret",
}

#: Words of the shell that are not variables (`$1`, `$@`) or are set by the shell itself.
SH_SPECIAL = {"LINENO", "PWD", "OLDPWD", "IFS", "RANDOM"}


# -- the scanners ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    name: str
    scope: str
    where: str


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "VAR"` / `NAME = env("SUFFIX")`: how the code spells a variable once and uses it often."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = _literal_name(node.value, {})
            if value is not None:
                found[node.targets[0].id] = value
    return found


def _is_env_helper(func: ast.expr) -> bool:
    """`env(...)` or `_brand.env(...)`: the helper that prefixes a suffix with `DYNFARM_`."""
    return (isinstance(func, ast.Name) and func.id == "env") or (isinstance(func, ast.Attribute) and func.attr == "env")


def _literal_name(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The variable a node names, when it can be told statically."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Call) and _is_env_helper(node.func) and len(node.args) == 1:
        suffix = _literal_name(node.args[0], constants)
        return _brand.ENV_PREFIX + suffix if suffix is not None else None
    return None


def _is_environ(node: ast.expr) -> bool:
    """`os.environ`, `environ`, `self.environ`, `store.environ`: a mapping of the process's environment."""
    return ast.unparse(node).split(".")[-1] == "environ"


def python_variables(source: str, where: str = "<snippet>") -> tuple[list[Found], list[str]]:
    """The environment variables a Python source names (read or `envvar=`), and the reads it cannot resolve."""
    tree = ast.parse(source)
    constants = _constants(tree)
    found: list[Found] = []
    dynamic: list[str] = []

    def note(node: ast.expr | None, at: ast.AST) -> None:
        if node is None:
            return
        name = _literal_name(node, constants)
        if name is None:
            if ast.unparse(node) not in dynamic:
                dynamic.append(ast.unparse(node))
        elif Found(name, PYTHON, f"{where}:{at.lineno}") not in found:
            found.append(Found(name, PYTHON, f"{where}:{at.lineno}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            reads = (
                isinstance(func, ast.Attribute)
                and func.attr in {"get", "pop", "setdefault"}
                and _is_environ(func.value)
            ) or (
                (isinstance(func, ast.Attribute) and func.attr == "getenv")
                or (isinstance(func, ast.Name) and func.id == "getenv")
            )
            if reads and node.args:
                note(node.args[0], node)
            elif _is_env_helper(func) and node.args:
                note(node, node)
            for keyword in node.keywords:
                if keyword.arg == "envvar":
                    note(keyword.value, node)
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            note(node.slice, node)
        elif isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
            note(node.left, node)
    return found, dynamic


_SH_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))")
_SH_ASSIGNMENT = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s\"'\\]*)")
_SH_FOR = re.compile(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
#: What may follow an assignment on its line without being the command it is a prefix of.
_SH_NOT_A_COMMAND = ("||", "&&", ";", "|", "#", "")


def _sh_assignments(line: str) -> tuple[list[str], bool]:
    """The `NAME=value` words that start a line, and whether a command follows them (`NAME=value command`).

    A `NAME=$(command ...)` is the script computing a value of its own: never a prefix, whatever it contains.
    """
    names: list[str] = []
    position = 0
    while match := _SH_ASSIGNMENT.match(line, position):
        if match.group(2).lstrip('"').startswith(("$(", "`")):
            return [match.group(1)], False
        names.append(match.group(1))
        position = match.end()
    continued = line.rstrip().endswith("\\")  # `NAME=value \` : the command is on the next line
    rest = line[position:].strip().rstrip("\\").strip()
    return names, bool(names) and (continued or not rest.startswith(_SH_NOT_A_COMMAND))


def sh_variables(text: str, where: str = "<snippet>") -> list[Found]:
    """The environment variables a POSIX script reads (`$VAR`, `${VAR}`) or sets for a command (`VAR=x command`).

    A name the script assigns on a line of its own (`NAME="x"`, a `for` variable) is the script's own, not the
    environment's: nothing about it is read from outside, unless the assignment reads it (`PATH="$X:$PATH"`).
    Whole-line comments are skipped.
    """
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    local = set(SH_SPECIAL)
    for line in lines:
        names, prefix = _sh_assignments(line)
        if not prefix:
            local.update(name for name in names if not re.search(rf"\$\{{?{name}\b", line))
        loop = _SH_FOR.match(line)
        if loop:
            local.add(loop.group(1))
    found: list[Found] = []
    for number, line in enumerate(lines, 1):
        for match in _SH_REF.finditer(line):
            name = match.group(1) or match.group(2)
            if name not in local:
                found.append(Found(name, SH, f"{where}:{number}"))
        names, prefix = _sh_assignments(line)
        if prefix:
            found += [Found(name, SH, f"{where}:{number}") for name in names]
    return found


_PS1_ENV = re.compile(r"\$\{?env:([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_PS1_GET = re.compile(r"GetEnvironmentVariable\(\s*[\"']([^\"']+)[\"']")


def ps1_variables(text: str, where: str = "<snippet>") -> list[Found]:
    """The environment variables a PowerShell script touches: `$env:NAME`, `${env:NAME}`, `GetEnvironmentVariable`."""
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    found: list[Found] = []
    for number, line in enumerate(lines, 1):
        for pattern in (_PS1_ENV, _PS1_GET):
            found += [Found(m.group(1), PS1, f"{where}:{number}") for m in pattern.finditer(line)]
    return found


def violations(found: list[Found]) -> list[str]:
    """The variables that are neither `DYNFARM_*` nor an allowed convention in the scope they appear in."""
    problems = []
    for item in found:
        name = item.name.upper() if item.scope == PS1 else item.name  # Windows names are case-insensitive
        if name.startswith(_brand.ENV_PREFIX):
            continue
        allowed = ALLOWED.get(name)
        if allowed is None:
            problems.append(f"{item.where}: {item.name} is neither {_brand.ENV_PREFIX}* nor an allowed convention")
        elif item.scope not in allowed.scopes:
            problems.append(f"{item.where}: {item.name} is allowed only in {allowed.scopes or 'no code of ours'}")
    return problems


def scan_repository() -> tuple[list[Found], list[str]]:
    found: list[Found] = []
    dynamic: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        names, unresolved = python_variables(path.read_text(), str(path.relative_to(ROOT)))
        found += names
        dynamic += unresolved
    found += sh_variables(INSTALL_SH.read_text(), "install.sh")
    found += ps1_variables(INSTALL_PS1.read_text(), "install.ps1")
    return found, dynamic


# -- the rule, on the repository ------------------------------------------------------------------------------------


def test_every_variable_is_ours_or_a_listed_one():
    found, _ = scan_repository()
    assert len(found) >= 10, "the scan found almost nothing: it is not reading the code"
    assert violations(found) == []


def test_the_scan_sees_the_variables_the_tool_is_known_to_read():
    found, _ = scan_repository()
    names = {item.name for item in found}
    assert {"DYNFARM_HOST", "DYNFARM_TOKEN", "DYNFARM_CONFIG_DIR", "DYNFARM_DEBUG", "CI", "NO_COLOR", "TERM"} <= names
    assert {"DYNFARM_INDEX_USERNAME", "DYNFARM_INDEX_PASSWORD"} <= names, "the install scripts' registry variables"


def test_a_read_whose_name_the_scan_cannot_see_is_one_the_person_chose():
    _, dynamic = scan_repository()
    assert sorted(set(dynamic) - set(DYNAMIC_READS)) == []


def test_the_allowlist_has_no_entry_that_nothing_uses():
    found, _ = scan_repository()
    seen = {(item.name.upper() if item.scope == PS1 else item.name, item.scope) for item in found}
    stale = [name for name, entry in ALLOWED.items() for scope in entry.scopes if (name, scope) not in seen]
    assert stale == [], f"allowed but no longer used (remove them): {stale}"


def test_the_proxy_variables_are_left_to_the_http_client():
    found, _ = scan_repository()
    assert {item.name for item in found} & {name for name, entry in ALLOWED.items() if not entry.scopes} == set()


def test_every_allowed_convention_has_a_reason():
    assert all(entry.reason.strip() for entry in ALLOWED.values())
    assert all(not name.startswith(_brand.ENV_PREFIX) for name in ALLOWED), "ours need no allowlist entry"


# -- the scanner can fail -------------------------------------------------------------------------------------------

#: Every way Python could read the removed token variable.
PYTHON_READS = [
    'import os\nos.environ.get("AGENT_FARM_TOKEN")',
    'import os\nos.environ["AGENT_FARM_TOKEN"]',
    'import os\nos.getenv("AGENT_FARM_TOKEN")',
    'from os import getenv\ngetenv("AGENT_FARM_TOKEN", "")',
    'def f(environ):\n    return environ.get("AGENT_FARM_TOKEN", "").strip()',
    'class S:\n    def f(self):\n        return self.environ.get("AGENT_FARM_TOKEN")',
    'import os\n"AGENT_FARM_TOKEN" in os.environ',
    'import click\n@click.option("--x", envvar="AGENT_FARM_TOKEN")\ndef f(): ...',
    'LEGACY = "AGENT_FARM_TOKEN"\ndef f(environ):\n    return environ.get(LEGACY, "")',
]


@pytest.mark.parametrize("snippet", PYTHON_READS)
def test_the_python_scan_flags_the_removed_token_variable_however_it_is_read(snippet):
    found, _ = python_variables(snippet)
    assert {item.name for item in found} == {"AGENT_FARM_TOKEN"}
    assert len(violations(found)) == 1


def test_the_python_scan_flags_a_name_that_only_looks_like_ours():
    found, _ = python_variables('import os\nos.environ.get("FARM_TOKEN")\nos.environ.get("dynfarm_token")')
    assert len(violations(found)) == 2


def test_the_python_scan_resolves_the_brand_helper_and_accepts_what_is_ours():
    source = (
        "from dynfarm import _brand\nimport os\n"
        'TOKEN = _brand.env("TOKEN")\n'
        'os.environ.get(TOKEN)\nos.environ.get(_brand.env("HOST"))\nos.environ.get("DYNFARM_DEBUG")\n'
        'os.environ.get("CI")\n'
    )
    found, dynamic = python_variables(source)
    assert sorted({item.name for item in found}) == ["CI", "DYNFARM_DEBUG", "DYNFARM_HOST", "DYNFARM_TOKEN"]
    assert dynamic == []
    assert violations(found) == []


def test_the_python_scan_reports_a_read_it_cannot_resolve_instead_of_ignoring_it():
    found, dynamic = python_variables("import os\ndef f(name):\n    return os.environ[name]")
    assert found == [] and dynamic == ["name"]


def test_the_python_scan_does_not_mistake_a_word_in_a_docstring_for_a_read():
    found, _ = python_variables(
        '"""AGENT_FARM_TOKEN is not read."""\nx = {"AGENT_FARM_TOKEN": 1}.get("AGENT_FARM_TOKEN")'
    )
    assert found == []


@pytest.mark.parametrize(
    "snippet",
    [
        'curl --header "$AGENT_FARM_TOKEN" "$URL"',
        'echo "${AGENT_FARM_URL:-}"',
        'if [ -n "${AGENT_FARM_TOKEN:-}" ]; then',
    ],
)
def test_the_shell_scan_flags_the_removed_variables_however_they_are_read(snippet):
    found = sh_variables(snippet)
    assert any(item.name.startswith("AGENT_FARM_") for item in found)
    assert any("AGENT_FARM_" in problem for problem in violations(found))


def test_the_shell_scan_flags_a_variable_set_for_a_command_and_leaves_the_scripts_own_names_alone():
    script = (
        'NAME="x"\nT="y"\nfor arg in "$@"; do echo "$arg $NAME $1"; done\n'
        'AGENT_FARM_TOKEN="$T" \\\n    uv tool install y\n'
    )
    found = sh_variables(script)
    assert [item.name for item in found] == ["AGENT_FARM_TOKEN"]
    assert len(violations(found)) == 1


def test_the_shell_scan_ignores_comments_and_accepts_ours():
    script = '# uses $AGENT_FARM_TOKEN\necho "${DYNFARM_INDEX_USERNAME:-}" "$HOME"\n'
    assert violations(sh_variables(script)) == []


def test_the_shell_scan_refuses_an_allowed_name_in_a_scope_that_may_not_use_it():
    assert len(violations([Found("USERPROFILE", SH, "x")])) == 1  # Windows' variable in a POSIX script
    assert violations([Found("USERPROFILE", PS1, "x")]) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "$env:AGENT_FARM_TOKEN",
        "if (${env:AGENT_FARM_URL}) { }",
        '[Environment]::GetEnvironmentVariable("AGENT_FARM_TOKEN")',
    ],
)
def test_the_powershell_scan_flags_the_removed_variables_however_they_are_read(snippet):
    found = ps1_variables(snippet)
    assert len(found) == 1 and found[0].name.startswith("AGENT_FARM_")
    assert len(violations(found)) == 1


def test_the_powershell_scan_ignores_comments_compares_names_case_insensitively_and_accepts_ours():
    script = (
        "<#\n$env:AGENT_FARM_TOKEN in help text\n#>\n# $env:AGENT_FARM_URL\n"
        '$env:Path = "$env:USERPROFILE;$env:DYNFARM_INDEX_PASSWORD"\n'
    )
    found = ps1_variables(script)
    assert [item.name for item in found] == ["Path", "USERPROFILE", "DYNFARM_INDEX_PASSWORD"]
    assert violations(found) == []


# -- the removed names are gone -------------------------------------------------------------------------------------

REMOVED = ("AGENT_FARM_TOKEN", "AGENT_FARM_URL")


def test_the_removed_variables_appear_nowhere_a_person_or_a_script_reads():
    scanned = sorted(SRC.rglob("*.py")) + [INSTALL_SH, INSTALL_PS1, ROOT / "README.md"]
    assert len(scanned) > 10
    for path in scanned:
        text = path.read_text()
        for name in REMOVED:
            assert name not in text, f"{path.relative_to(ROOT)} still mentions {name}"


def test_the_brand_module_keeps_no_name_for_them():
    assert not [name for name in dir(_brand) if name.startswith("LEGACY_ENV")]
    assert _brand.LEGACY_KEYRING_SERVICE == "agent-farm-token", "the keychain import is not an environment variable"
