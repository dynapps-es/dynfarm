"""The tool's name lives in one constant, and the command people type agrees with it.

The user asked for the name to be configurable (`dynfarm` today). That only holds if nothing else hard-codes it: the
console script key in `pyproject.toml` is the one place outside `_brand.py` that must change too, so a test pins it.
"""

import tomllib
from pathlib import Path

from click.testing import CliRunner

from dynfarm import __version__, _brand
from dynfarm.cli import cli

ROOT = Path(__file__).resolve().parents[1]


def test_the_console_script_is_named_after_the_brand_constant():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert list(scripts) == [_brand.NAME]
    assert scripts[_brand.NAME] == "dynfarm.cli:main"


def test_environment_variables_are_derived_from_the_brand_name():
    assert _brand.env("TOKEN") == _brand.NAME.upper() + "_TOKEN"
    assert _brand.env("HOST") == _brand.NAME.upper() + "_HOST"


def test_the_keyring_service_is_the_brand_name():
    assert _brand.KEYRING_SERVICE == _brand.NAME


def test_version_flag_prints_the_package_version():
    result = CliRunner().invoke(cli, ["--version"], prog_name=_brand.NAME)
    assert result.exit_code == 0
    assert result.output.strip() == f"{_brand.NAME} {__version__}"


def test_help_lists_only_commands_that_load():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"], prog_name=_brand.NAME)
    assert result.exit_code == 0
    with cli.make_context(_brand.NAME, [], resilient_parsing=True) as ctx:
        for name in cli.list_commands(ctx):
            assert cli.get_command(ctx, name) is not None, name


def test_user_agent_names_the_tool_and_its_version():
    assert _brand.user_agent().startswith(f"{_brand.NAME}/{__version__} (python ")
