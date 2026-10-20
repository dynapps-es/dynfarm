"""A tag becomes an installable release, and nothing in the way in can leak the token that opens the registry.

Why these tests exist: the release path runs rarely (once per version) and in places a developer does not watch — a
tag pipeline, a stranger's laptop, a PowerShell prompt — so it must be pinned where it is cheap:

- the version has one source, and the gate that stops a tag from publishing a package that disagrees with it can fail;
- the pipeline's `publish` and `release` jobs run on tags and only on tags, and publish with the job token;
- the install scripts agree with each other and with the pipeline, never put a credential in a URL, and refuse a
  version that is not a version before it reaches a shell, a requirement or a git ref;
- `install.sh` is *run*, against a fake `uv`, `git` and `curl`, because "it passes `sh -n`" says nothing about which
  arguments reach `uv`; `install.ps1` has no interpreter here or in CI, so it is read, not run;
- the built wheel carries the brand module, the new command and the entry point;
- the README documents what the code does (every command, every exit code, every environment variable), because a
  user guide that drifts from the tool is the first thing a person trips on.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import dynfarm
from dynfarm import _brand
from dynfarm.cli import COMMANDS, cli

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([abc]\d+|rc\d+|\.post\d+|\.dev\d+)?$")

# What a credential inside a URL looks like: `https://user:secret@host`, `https://token@host`, or a token in the query.
CREDENTIAL_IN_URL = re.compile(
    r"""[a-z+]+://[^/\s"'`]*@"""  # userinfo before the host
    r"""|[?&](?:private_token|access_token|token|password)="""  # token in the query string
    r"""|oauth2:|gitlab-ci-token:""",  # the usual GitLab "user:token" pairs
    re.IGNORECASE,
)

FAKE_TOKEN = "fake-registry-token-0123456789"


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check_tag():
    return load_tool("check_tag")


def ci() -> dict:
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())


# -- the version ---------------------------------------------------------------------------------------------------


def test_the_version_has_one_source_which_the_build_reads():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"], "a literal version in pyproject.toml would be a second source"
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/dynfarm/__init__.py"
    assert VERSION_RE.match(dynfarm.__version__), dynfarm.__version__


def test_the_first_release_is_0_1_0_and_the_changelog_says_what_it_holds(check_tag):
    assert dynfarm.__version__ == "0.1.0"
    section = check_tag.changelog_section(dynfarm.__version__, (ROOT / "CHANGELOG.md").read_text())
    assert section and "dynfarm auth" in section and "dynfarm credential" in section


# -- the gate between a tag and the registry -----------------------------------------------------------------------


def run_check_tag(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_tag.py"), *args], capture_output=True, text=True, check=False
    )


def test_a_tag_that_is_the_package_version_is_accepted_and_its_notes_are_written(tmp_path):
    notes = tmp_path / "notes.md"
    run = run_check_tag(f"v{dynfarm.__version__}", "--notes", str(notes))
    assert run.returncode == 0, run.stderr
    text = notes.read_text()
    assert text.startswith("First release") and "## [" not in text  # the section's body, not the next version's


@pytest.mark.parametrize("tag", ["v9.9.9", "v0.1.0.1", "0.1.0", "release-0.1.0", "v"])
def test_a_tag_that_is_not_the_package_version_is_refused_before_anything_is_published(tag):
    run = run_check_tag(tag)
    assert run.returncode == 1
    assert dynfarm.__version__ in run.stderr and repr(tag) in run.stderr


def test_a_version_without_a_changelog_section_is_refused(check_tag):
    problems = check_tag.check("v1.2.0", "1.2.0", "# Changelog\n\n## [1.1.0] - 2026-01-01\n\nOld.\n")
    assert len(problems) == 1 and "no '## [1.2.0]' section" in problems[0]


def test_a_changelog_section_with_nothing_in_it_is_refused(check_tag):
    problems = check_tag.check("v1.2.0", "1.2.0", "## [1.2.0] - unreleased\n\n## [1.1.0] - 2026-01-01\n\nOld.\n")
    assert problems and "empty" in problems[0]


def test_the_notes_are_only_the_section_of_the_version_asked_for(check_tag):
    text = (
        "# Changelog\n\n## [1.2.0] - 2026-02-02\n\nNew thing.\n\n### Added\n\n- x\n\n## [1.1.0] - 2026-01-01\n\nOld.\n"
    )
    assert check_tag.changelog_section("1.2.0", text) == "New thing.\n\n### Added\n\n- x"
    assert check_tag.changelog_section("1.1.0", text) == "Old."
    assert check_tag.changelog_section("1.0.0", text) is None


def test_a_tag_is_compared_with_the_version_in_the_source_file_without_importing_the_package(check_tag, tmp_path):
    source = tmp_path / "__init__.py"
    source.write_text('"""doc"""\n\n__version__ = "3.4.5"\n')
    assert check_tag.package_version(source) == "3.4.5"
    source.write_text('"""doc"""\n')
    with pytest.raises(ValueError, match="__version__"):
        check_tag.package_version(source)


# -- the pipeline --------------------------------------------------------------------------------------------------


def test_publish_and_release_run_on_tags_and_on_nothing_else():
    pipeline = ci()
    for name in ("publish", "release"):
        rules = pipeline[name]["rules"]
        assert rules, name
        for rule in rules:
            assert rule.get("if", "").startswith("$CI_COMMIT_TAG =~ /^v"), (
                f"{name} has a rule that is not a v* tag: {rule}"
            )
            assert rule.get("when", "on_success") == "on_success"
        assert pipeline[name]["stage"] == "release"
    assert [name for name, job in pipeline.items() if isinstance(job, dict) and job.get("stage") == "release"] == [
        "publish",
        "release",
    ]


def test_the_release_stage_comes_after_every_gate_so_a_red_tree_is_never_published():
    stages = ci()["stages"]
    assert stages[-1] == "release" and {"lint", "test"} < set(stages)


def test_publish_checks_the_tag_then_uploads_with_the_job_token_and_no_stored_secret():
    job = ci()["publish"]
    script = [line for line in job["script"] if isinstance(line, str)]
    check = next(i for i, line in enumerate(script) if "tools/check_tag.py" in line and "$CI_COMMIT_TAG" in line)
    upload = next(i for i, line in enumerate(script) if "twine upload" in line)
    assert check < upload, "the tag must be checked before anything is uploaded"
    assert "packages/pypi" in script[upload] and "${CI_PROJECT_ID}" in script[upload]
    assert job["variables"] == {"TWINE_USERNAME": "gitlab-ci-token", "TWINE_PASSWORD": "$CI_JOB_TOKEN"}
    assert not CREDENTIAL_IN_URL.search("\n".join(script)), "token in a URL"


def test_the_release_takes_its_notes_and_links_from_publish():
    pipeline = ci()
    release = pipeline["release"]
    assert release["needs"] == ["publish"]
    assert release["release"]["tag_name"] == "$CI_COMMIT_TAG"
    assert release["release"]["description"] == "release-notes.md"
    assert "release-notes.md" in pipeline["publish"]["artifacts"]["paths"]
    urls = [link["url"] for link in release["release"]["assets"]["links"]]
    assert any(url.endswith("/install.sh") for url in urls) and any(url.endswith("/install.ps1") for url in urls)
    assert "$FILE_WHL_URL" in urls and "$FILE_TAR_GZ_URL" in urls
    assert pipeline["publish"]["artifacts"]["reports"]["dotenv"] == "release.env"
    for url in urls:
        assert not CREDENTIAL_IN_URL.search(url), url


def test_the_ordinary_jobs_still_run_on_a_tag_so_the_release_waits_for_them():
    pipeline = ci()
    assert any(rule.get("if") == "$CI_COMMIT_TAG" for rule in pipeline["workflow"]["rules"])
    for name in ("lint", "test", "e2e", "package"):
        assert pipeline[name]["extends"] == [".python", ".not_scheduled"], name
    assert pipeline[".not_scheduled"]["rules"][-1] == {"when": "on_success"}


# -- the install scripts -------------------------------------------------------------------------------------------


def assignments(text: str, pattern: str) -> list[str]:
    return re.findall(pattern, text, re.MULTILINE)


def test_both_install_scripts_use_the_same_registry_git_address_and_index_name():
    sh, ps1 = INSTALL_SH.read_text(), INSTALL_PS1.read_text()
    registry = [
        assignments(sh, r'^REGISTRY_URL="([^"]+)"'),
        assignments(ps1, r'^\$RegistryUrl = "([^"]+)"'),
    ]
    git = [assignments(sh, r'^GIT_URL="([^"]+)"'), assignments(ps1, r'^\$GitUrl = "([^"]+)"')]
    index = [assignments(sh, r'^INDEX_NAME="([^"]+)"'), assignments(ps1, r'^\$IndexName = "([^"]+)"')]
    name = [assignments(sh, r'^NAME="([^"]+)"'), assignments(ps1, r'^\$Name = "([^"]+)"')]
    for found in (registry, git, index, name):
        assert found[0] and found[0] == found[1], found
    assert registry[0][0].startswith("https://gitlab.dynapps.be/api/v4/projects/") and registry[0][0].endswith(
        "/packages/pypi/simple"
    )
    assert git[0][0] == "https://gitlab.dynapps.be/infra-es/farm-ai/cli.git"
    assert name[0][0] == _brand.NAME
    # The person sets DYNFARM_INDEX_*; the scripts and the docs name the same two. uv derives the variables it reads
    # from the index's name, and only the scripts mention those.
    for text in (sh, ps1, (ROOT / "README.md").read_text()):
        assert "DYNFARM_INDEX_USERNAME" in text and "DYNFARM_INDEX_PASSWORD" in text
    uv_variable = f"UV_INDEX_{index[0][0].upper().replace('-', '_')}_"
    for text in (sh, ps1):
        assert uv_variable + "USERNAME" in text and uv_variable + "PASSWORD" in text
    assert "UV_INDEX" not in (ROOT / "README.md").read_text()


def test_the_registry_the_scripts_read_is_the_project_the_pipeline_publishes_to():
    project_id = re.search(r"/projects/(\d+)/", INSTALL_SH.read_text()).group(1)
    assert project_id == "4378"
    readme = (ROOT / "README.md").read_text()
    assert f"projects/{project_id}/packages/pypi/simple" in readme
    assert "${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/packages/pypi" in "\n".join(ci()["publish"]["script"])


@pytest.mark.parametrize(
    "path", [INSTALL_SH, INSTALL_PS1, ROOT / ".gitlab-ci.yml", ROOT / "README.md"], ids=lambda p: p.name
)
def test_no_install_path_puts_a_credential_in_a_url(path):
    text = path.read_text().replace("gitlab-ci-token", "")  # a user name in a variable is not a credential in a URL
    assert CREDENTIAL_IN_URL.findall(text) == []


def test_install_ps1_never_exits_the_callers_terminal_and_checks_what_it_runs():
    text = INSTALL_PS1.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert re.search(r"^\s*exit\b", code, re.MULTILINE) is None  # `throw`: it is often run inside an open terminal
    assert '$ErrorActionPreference = "Stop"' in text
    assert "[string]$Version" in text and "--index" in text and "--reinstall" in text
    assert "git+$GitUrl@$Tag" in text
    assert text.count("{") == text.count("}") and text.count("(") == text.count(")")  # a missing brace is not a script
    assert "'$Version' is not a version like 0.1.0" in text


def shell() -> str:
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX sh on this machine (Windows): install.ps1 is its installer")
    return found


def test_install_sh_is_valid_posix_shell():
    run = subprocess.run([shell(), "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert INSTALL_SH.read_text().startswith("#!/bin/sh\n")


class Machine:
    """A machine where `uv`, `git` and `curl` are scripts that write down what they were asked, and nothing else exists.

    The PATH holds only these and symlinks to the few system tools install.sh uses, so a real `uv` on the developer's
    machine cannot answer instead of the fake one.
    """

    TOOLS = ("sh", "sed", "grep", "mktemp", "rm", "cat", "mkdir", "cp", "chmod", "env", "tr")

    def __init__(self, tmp_path: Path, *, with_uv: bool = True, tags: str = "") -> None:
        self.home = tmp_path / "home"
        self.bin = tmp_path / "fakebin"
        self.system = tmp_path / "system"
        self.tool_bin = tmp_path / "toolbin"  # where `uv tool dir --bin` says tools go; not on PATH
        self.log = tmp_path / "calls.log"
        self.tags = tmp_path / "tags.txt"
        for directory in (self.home, self.bin, self.system, self.tool_bin):
            directory.mkdir()
        self.log.write_text("")
        self.tags.write_text(tags)
        for tool in self.TOOLS:
            (self.system / tool).symlink_to(shutil.which(tool))
        self.uv_script = self._script(
            "uv-source",
            """
            echo "uv $*" >> "$FAKE_LOG"
            echo "uv-env $(env | grep '^UV_INDEX_' | sed 's/=.*//' | tr '\\n' ' ')" >> "$FAKE_LOG"
            if [ "$1 $2 $3" = "tool dir --bin" ]; then echo "$FAKE_TOOL_BIN"; fi
            [ -z "${FAKE_UV_FAILS:-}" ] || exit 1
            """,
            install=False,
        )
        if with_uv:
            shutil.copy(self.uv_script, self.bin / "uv")
        self._script(
            "git",
            """
            echo "git $*" >> "$FAKE_LOG"
            [ -z "${FAKE_GIT_FAILS:-}" ] || exit 128
            for last; do :; done
            case "$last" in
                refs/tags/*) grep -q "refs/tags/${last#refs/tags/}\\$" "$FAKE_TAGS" || exit 2 ;;
                *) cat "$FAKE_TAGS" ;;
            esac
            """,
        )
        self._script(
            "curl",
            """
            echo "curl $*" >> "$FAKE_LOG"
            [ -z "${FAKE_CURL_FAILS:-}" ] || exit 22
            while [ "$#" -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
            printf '%s\\n' 'mkdir -p "$HOME/.local/bin"' 'cp "$FAKE_UV_SOURCE" "$HOME/.local/bin/uv"' \\
                'chmod +x "$HOME/.local/bin/uv"' > "$out"
            """,
        )
        self._script(
            "dynfarm",
            """
            echo "dynfarm-env $(env | grep '^UV_INDEX_' | sed 's/=.*//' | tr '\\n' ' ')" >> "$FAKE_LOG"
            echo "dynfarm 0.1.0"
            """,
            directory=self.tool_bin,
        )

    def _script(self, name: str, body: str, *, install: bool = True, directory: Path | None = None) -> Path:
        directory = directory or (self.bin if install else self.bin.parent)
        path = directory / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body).strip("\n") + "\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def run(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{self.system}",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_TAGS": str(self.tags),
            "FAKE_TOOL_BIN": str(self.tool_bin),
            "FAKE_UV_SOURCE": str(self.uv_script),
            **(extra_env or {}),
        }
        return subprocess.run([shell(), str(INSTALL_SH), *args], capture_output=True, text=True, env=env, check=False)

    @property
    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if not line.startswith(("uv-env", "dynfarm-env"))]

    @property
    def uv_calls(self) -> list[str]:
        return [line for line in self.calls if line.startswith("uv ")]

    @property
    def uv_credential_variables(self) -> str:
        return " ".join(line for line in self.log.read_text().splitlines() if line.startswith("uv-env"))

    @property
    def installed_command_credential_variables(self) -> str:
        """The `UV_INDEX_*` names the installed `dynfarm` (run by the script's final check) found in its environment."""
        return " ".join(line for line in self.log.read_text().splitlines() if line.startswith("dynfarm-env"))


REGISTRY_URL = assignments(INSTALL_SH.read_text(), r'^REGISTRY_URL="([^"]+)"')[0]
GIT_URL = assignments(INSTALL_SH.read_text(), r'^GIT_URL="([^"]+)"')[0]
REGISTRY_ENV = {"DYNFARM_INDEX_USERNAME": "alice", "DYNFARM_INDEX_PASSWORD": FAKE_TOKEN}
TAGS = "c1\trefs/tags/v0.2.0rc1\nc2\trefs/tags/v0.1.1\nc3\trefs/tags/v0.1.0\n"


def test_with_registry_credentials_uv_gets_the_index_by_name_and_the_token_only_in_its_environment(tmp_path):
    machine = Machine(tmp_path)
    run = machine.run("0.1.0", extra_env=REGISTRY_ENV)
    assert run.returncode == 0, run.stderr
    assert machine.uv_calls[0] == f"uv tool install --reinstall --index dynfarm={REGISTRY_URL} dynfarm==0.1.0"
    assert not any(call.startswith("git ") for call in machine.calls), "the registry path must not need git"
    assert FAKE_TOKEN not in machine.log.read_text() + run.stdout + run.stderr
    assert "UV_INDEX_DYNFARM_USERNAME" in machine.uv_credential_variables  # uv sees them, as the variables it reads
    assert "UV_INDEX_DYNFARM_PASSWORD" in machine.uv_credential_variables
    assert machine.installed_command_credential_variables.strip() == "dynfarm-env"  # nothing else the script runs
    assert run.stdout.strip().splitlines()[-3:-1] == ["dynfarm 0.1.0", ""]  # the check ran the installed command


def test_the_uv_credential_variables_are_not_read_from_the_callers_environment(tmp_path):
    machine = Machine(tmp_path, tags=TAGS)
    uv_names = {"UV_INDEX_DYNFARM_USERNAME": "alice", "UV_INDEX_DYNFARM_PASSWORD": FAKE_TOKEN}
    run = machine.run(extra_env=uv_names)
    assert run.returncode == 0, run.stderr
    assert "--index" not in machine.uv_calls[0] and f"git+{GIT_URL}@v0.1.1" in machine.uv_calls[0]


def test_the_scripts_never_export_the_uv_credential_variables_to_the_persons_shell():
    sh, ps1 = INSTALL_SH.read_text(), INSTALL_PS1.read_text()
    code_sh = "\n".join(line for line in sh.splitlines() if not line.lstrip().startswith("#"))
    assert "export UV_INDEX" not in code_sh
    assert re.search(r"^\s*UV_INDEX_DYNFARM_USERNAME=.*UV_INDEX_DYNFARM_PASSWORD=.*\\$", code_sh, re.MULTILINE)
    # PowerShell has no per-command environment: they are set around the uv call and put back in a `finally`.
    assert re.search(
        r"finally \{[^}]*\$env:UV_INDEX_DYNFARM_USERNAME = \$Saved[^}]*\$env:UV_INDEX_DYNFARM_PASSWORD", ps1
    )


def test_without_a_version_the_registry_path_installs_the_latest(tmp_path):
    machine = Machine(tmp_path)
    assert machine.run(extra_env=REGISTRY_ENV).returncode == 0
    assert machine.uv_calls[0].endswith(f"--index dynfarm={REGISTRY_URL} dynfarm")


def test_with_half_of_the_registry_credentials_it_falls_back_to_git_instead_of_failing_late(tmp_path):
    machine = Machine(tmp_path, tags=TAGS)
    assert machine.run(extra_env={"DYNFARM_INDEX_USERNAME": "alice"}).returncode == 0
    assert "--index" not in machine.uv_calls[0] and f"git+{GIT_URL}@v0.1.1" in machine.uv_calls[0]


def test_without_credentials_it_installs_the_newest_final_release_from_git_never_a_prerelease(tmp_path):
    machine = Machine(tmp_path, tags=TAGS)
    run = machine.run()
    assert run.returncode == 0, run.stderr
    assert machine.uv_calls[0] == f"uv tool install --reinstall git+{GIT_URL}@v0.1.1"
    assert any("ls-remote" in call and "--sort=-v:refname" in call for call in machine.calls)


@pytest.mark.parametrize("given", ["0.1.0", "v0.1.0"])
def test_a_version_with_or_without_the_v_installs_that_tag_from_git(tmp_path, given):
    machine = Machine(tmp_path, tags=TAGS)
    assert machine.run(given).returncode == 0
    assert machine.uv_calls[0] == f"uv tool install --reinstall git+{GIT_URL}@v0.1.0"


def test_a_tag_that_does_not_exist_stops_before_uv_is_asked_to_install_anything(tmp_path):
    machine = Machine(tmp_path, tags=TAGS)
    run = machine.run("7.7.7")
    assert run.returncode == 1 and "v7.7.7" in run.stderr
    assert machine.uv_calls == []


def test_git_that_cannot_read_the_project_says_how_to_sign_in_and_installs_nothing(tmp_path):
    machine = Machine(tmp_path, tags=TAGS)
    run = machine.run(extra_env={"FAKE_GIT_FAILS": "1"})
    assert run.returncode == 1 and "sign in" in run.stderr
    assert machine.uv_calls == []


def test_a_project_with_no_final_release_yet_is_an_error_not_an_install_of_nothing(tmp_path):
    machine = Machine(tmp_path, tags="c1\trefs/tags/v0.1.0rc1\n")
    run = machine.run()
    assert run.returncode == 1 and "no release tag" in run.stderr
    assert machine.uv_calls == []


@pytest.mark.parametrize("bad", ["x;rm -rf ~", "$(id)", "0.1.0 --index evil=http://x", "v", "latest", "../0.1"])
def test_a_version_that_is_not_a_version_is_refused_before_anything_runs(tmp_path, bad):
    machine = Machine(tmp_path, tags=TAGS)
    run = machine.run(bad, extra_env=REGISTRY_ENV)
    assert run.returncode == 1 and "not a version" in run.stderr
    assert machine.calls == []


def test_two_versions_or_an_unknown_option_are_refused(tmp_path):
    machine = Machine(tmp_path)
    assert machine.run("0.1.0", "0.2.0").returncode == 1
    refused = machine.run("--token", FAKE_TOKEN)
    assert refused.returncode == 1 and "unknown option" in refused.stderr
    assert FAKE_TOKEN not in refused.stdout + refused.stderr  # a secret given by mistake is not echoed back
    assert machine.calls == []


def test_help_prints_the_usage_and_changes_nothing(tmp_path):
    machine = Machine(tmp_path)
    run = machine.run("--help")
    assert run.returncode == 0 and "Usage: sh install.sh [VERSION]" in run.stdout
    assert machine.calls == []


def test_a_missing_uv_is_installed_from_astral_first_and_then_used(tmp_path):
    machine = Machine(tmp_path, with_uv=False, tags=TAGS)
    run = machine.run()
    assert run.returncode == 0, run.stderr
    curl = next(call for call in machine.calls if call.startswith("curl "))
    assert "https://astral.sh/uv/install.sh" in curl and "-o " in curl  # to a file first, never `| sh`
    assert (machine.home / ".local" / "bin" / "uv").exists()
    assert machine.uv_calls and machine.uv_calls[0].startswith("uv tool install")


def test_a_failed_uv_download_stops_the_install_and_says_so(tmp_path):
    machine = Machine(tmp_path, with_uv=False, tags=TAGS)
    run = machine.run(extra_env={"FAKE_CURL_FAILS": "1"})
    assert run.returncode == 1 and "uv installer" in run.stderr
    assert machine.uv_calls == []


def test_a_failed_uv_install_is_reported_and_is_not_followed_by_a_success_message(tmp_path):
    machine = Machine(tmp_path, tags=TAGS)
    run = machine.run(extra_env={**REGISTRY_ENV, "FAKE_UV_FAILS": "1"})
    assert run.returncode == 1 and "check the user name and the token" in run.stderr
    assert "Next:" not in run.stdout and FAKE_TOKEN not in run.stdout + run.stderr


def test_when_the_tool_directory_is_not_on_the_path_it_says_how_to_fix_it_and_still_runs_the_check(tmp_path):
    machine = Machine(tmp_path, tags=TAGS)
    run = machine.run()
    assert "uv tool update-shell" in run.stdout and "dynfarm 0.1.0" in run.stdout


# -- completion ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shell_name", "marker"),
    [("zsh", "#compdef dynfarm"), ("bash", "_dynfarm_completion"), ("fish", "complete --no-files --command dynfarm")],
)
def test_completion_prints_a_script_for_each_shell_and_nothing_else_on_stdout(shell_name, marker):
    result = CliRunner().invoke(cli, ["completion", shell_name], prog_name=_brand.NAME)
    assert result.exit_code == 0, result.output
    assert marker in result.stdout and "_DYNFARM_COMPLETE" in result.stdout


def test_completion_refuses_a_shell_it_has_no_script_for():
    result = CliRunner().invoke(cli, ["completion", "powershell"], prog_name=_brand.NAME)
    assert result.exit_code == 2 and "zsh" in result.output


def test_the_script_asks_dynfarm_for_candidates_under_the_name_click_listens_on():
    """The script is worth nothing if its variable is not the one the program answers to: ask it as the shell would."""
    script = CliRunner().invoke(cli, ["completion", "zsh"], prog_name=_brand.NAME).stdout
    variable = re.search(r"(_[A-Z_]+_COMPLETE)=zsh_complete", script).group(1)
    answer = CliRunner().invoke(
        cli, [], prog_name=_brand.NAME, env={variable: "zsh_complete", "COMP_WORDS": "dynfarm ta", "COMP_CWORD": "1"}
    )
    assert answer.stdout.splitlines()[:2] == ["plain", "task"], answer.output


def test_completion_needs_no_login_no_farm_and_no_keyring(memory_keyring):
    result = CliRunner().invoke(cli, ["completion", "zsh"], prog_name=_brand.NAME)
    assert result.exit_code == 0 and memory_keyring.calls == []


# -- the built package ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wheel")
    run = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    (built,) = out.glob("*.whl")
    return built


def test_the_wheel_is_named_for_the_version_and_carries_the_brand_the_commands_and_the_entry_point(wheel):
    assert wheel.name == f"dynfarm-{dynfarm.__version__}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "dynfarm/_brand.py" in names
        for target in COMMANDS.values():
            module = target.partition(":")[0].replace(".", "/") + ".py"
            assert module in names, f"{module} is missing from the wheel"
        entry_points = next(archive.read(n).decode() for n in names if n.endswith(".dist-info/entry_points.txt"))
        metadata = next(archive.read(n).decode() for n in names if n.endswith(".dist-info/METADATA"))
    assert "[console_scripts]" in entry_points and f"{_brand.NAME} = dynfarm.cli:main" in entry_points
    assert f"Version: {dynfarm.__version__}" in metadata and "Requires-Python: >=3.11" in metadata
    assert "Requires-Dist: click==" in metadata  # the exact pins travel with the package


# -- the documentation ---------------------------------------------------------------------------------------------


def readme() -> str:
    return (ROOT / "README.md").read_text()


def test_the_readme_documents_every_command_of_the_tool():
    text = readme()
    for name in COMMANDS:
        assert f"`dynfarm {name}" in text, f"`dynfarm {name}` is not in the command list of the README"


def test_the_readme_lists_every_exit_code_a_script_can_see():
    section = readme().split("### Exit codes", 1)[1].split("###", 1)[0]
    for code in ("0", "1", "2", "4", "130"):
        assert f"| `{code}` |" in section, f"exit code {code} is not in the README's table"
    assert "Ctrl-C" in section


def test_the_readme_lists_every_environment_variable_the_code_reads():
    sources = "\n".join(path.read_text() for path in (ROOT / "src" / "dynfarm").rglob("*.py"))
    names = {_brand.env(suffix) for suffix in re.findall(r'\benv\("([A-Z_]+)"\)', sources)}
    names |= {"NO_COLOR", "CI", "DYNFARM_INDEX_USERNAME", "DYNFARM_INDEX_PASSWORD"}
    assert {"DYNFARM_HOST", "DYNFARM_TOKEN", "DYNFARM_CONFIG_DIR", "DYNFARM_DEBUG"} <= names
    section = readme().split("### Environment variables", 1)[1].split("###", 1)[0]
    missing = sorted(name for name in names if f"`{name}`" not in section)
    assert missing == [], f"not in the README's environment table: {missing}"


def test_the_readme_has_the_way_in_the_way_out_and_the_ci_recipe():
    text = readme()
    for needle in (
        "sh cli/install.sh",
        ".\\cli\\install.ps1",
        "uv tool install",
        "pipx install",
        "## First steps",
        "### Several farms",
        "## Upgrade and uninstall",
        "uv tool uninstall dynfarm",
        "dynfarm completion",
        "--exit-status",
    ):
        assert needle in text, needle


def test_the_code_and_the_docs_carry_no_reference_to_the_plan_that_built_them():
    scanned = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "install.sh", "install.ps1")]
    scanned += sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))
    scanned += sorted((ROOT / "tools").glob("*.py")) + [ROOT / ".gitlab-ci.yml"]
    plan_reference = re.compile(r"\b(?:D\d{1,2}|[Pp]hase \d+|research Q\d)\b")
    this_file = Path(__file__).resolve()
    for path in scanned:
        if path.resolve() == this_file:
            continue  # it holds the pattern it looks for
        text = path.read_text()
        assert not plan_reference.search(text), f"{path.relative_to(ROOT)} refers to a plan phase or decision"
