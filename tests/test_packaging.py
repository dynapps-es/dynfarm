"""A tag becomes a public PyPI release through a workflow that cannot publish by accident, and installs openly.

Why these tests exist: the release path runs rarely (once per version) and in places a developer does not watch — a
GitHub runner, a stranger's laptop, a PowerShell prompt — so it must be pinned where it is cheap:

- the version has one source, and the gate that stops a tag from publishing a package that disagrees with it can fail;
- the release workflow starts on a `v*` tag and on nothing else, checks the tag first, runs the whole of `ci.yml` (the
  suite on Linux, Windows and macOS) before it builds, hands PyPI an OIDC identity from one job only (Trusted
  Publishing: there is no token to leak), installs what it published on each system, and every action it uses is pinned
  to a commit, because a tag of somebody else's action can be moved to run code with that identity;
- `ci.yml`, which runs on `main` of the public mirror and on every release, reads the repository and nothing else,
  on the three systems the tool is installed on, with the same pins;
- GitLab, where the project is developed, publishes nothing: it only shows a bad tag red;
- the install scripts put `dynfarm` from PyPI on the machine, never handle a credential, and refuse a version that is
  not a version before it reaches a shell or a requirement;
- `install.sh` is *run*, against a fake `uv` and `curl`, because "it passes `sh -n`" says nothing about which arguments
  reach `uv`; `install.ps1` has no interpreter here or in CI, so it is read, not run;
- nothing public points at the private GitLab: a link a reader cannot open, or a hostname that is not theirs to see;
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
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([abc]\d+|rc\d+|\.post\d+|\.dev\d+)?$")

# What a credential inside a URL looks like: `https://user:secret@host`, `https://token@host`, or a token in the query.
CREDENTIAL_IN_URL = re.compile(
    r"""[a-z+]+://[^/\s"'`]*@"""  # userinfo before the host
    r"""|[?&](?:private_token|access_token|token|password)="""  # token in the query string
    r"""|oauth2:|gitlab-ci-token:""",  # the usual GitLab "user:token" pairs
    re.IGNORECASE,
)

FAKE_TOKEN = "fake-token-0123456789"


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check_tag():
    return load_tool("check_tag")


def ci() -> dict:
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))


def workflow(path: Path = WORKFLOW) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def ci_workflow() -> dict:
    return workflow(CI_WORKFLOW)


# -- the version ---------------------------------------------------------------------------------------------------


def test_the_version_has_one_source_which_the_build_reads():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in project["project"], "a literal version in pyproject.toml would be a second source"
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/dynfarm/__init__.py"
    assert VERSION_RE.match(dynfarm.__version__), dynfarm.__version__


def test_the_first_release_is_0_1_0_and_the_changelog_says_what_it_holds(check_tag):
    assert dynfarm.__version__ == "0.1.0"
    section = check_tag.changelog_section(dynfarm.__version__, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert section and "dynfarm auth" in section and "dynfarm credential" in section


# -- the gate between a tag and PyPI --------------------------------------------------------------------------------


def run_check_tag(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_tag.py"), *args], capture_output=True, text=True, check=False
    )


def test_a_tag_that_is_the_package_version_is_accepted_and_its_notes_are_written(tmp_path):
    notes = tmp_path / "notes.md"
    run = run_check_tag(f"v{dynfarm.__version__}", "--notes", str(notes))
    assert run.returncode == 0, run.stderr
    text = notes.read_text(encoding="utf-8")
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
    source.write_text('"""doc"""\n\n__version__ = "3.4.5"\n', encoding="utf-8")
    assert check_tag.package_version(source) == "3.4.5"
    source.write_text('"""doc"""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="__version__"):
        check_tag.package_version(source)


# -- the GitLab pipeline: it publishes nothing ----------------------------------------------------------------------


def test_gitlab_publishes_nothing_it_only_checks_the_tag_so_a_bad_one_is_red_there_too():
    pipeline = ci()
    assert "publish" not in pipeline and "release" not in pipeline
    assert "release" not in pipeline["stages"]
    job = pipeline["check-tag"]
    assert job["rules"] == [{"if": "$CI_COMMIT_TAG =~ /^v/"}]
    assert job["script"] == ['python tools/check_tag.py "$CI_COMMIT_TAG"']
    text = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    for leftover in ("twine upload", "TWINE_", "CI_JOB_TOKEN", "release-cli", "packages/pypi"):
        assert leftover not in text, f"{leftover!r} would publish or release from GitLab"


def test_the_ordinary_jobs_still_run_on_a_tag_so_a_bad_tag_is_seen_with_the_whole_pipeline():
    pipeline = ci()
    assert any(rule.get("if") == "$CI_COMMIT_TAG" for rule in pipeline["workflow"]["rules"])
    for name in ("lint", "test", "e2e", "package"):
        assert pipeline[name]["extends"] == [".python", ".not_scheduled"], name
    assert pipeline[".not_scheduled"]["rules"][-1] == {"when": "on_success"}


# -- the workflows (GitHub, on the public mirror) -------------------------------------------------------------------

#: The actions the workflows may use. A new one is a decision: the release ones run with the identity PyPI trusts, and
#: `ci.yml` is called by them.
RELEASE_ACTIONS = {
    "actions/checkout",
    "actions/setup-python",
    "actions/upload-artifact",
    "actions/download-artifact",
    "pypa/gh-action-pypi-publish",
}
CI_ACTIONS = {"actions/checkout", "actions/setup-python"}
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
THREE_SYSTEMS = ["ubuntu-latest", "windows-latest", "macos-latest"]
#: The release calls the CI workflow of the same commit by its path: a local reference, nothing to pin.
CI_CALL = "./.github/workflows/ci.yml"
LINT_ONLY_ONCE = "matrix.os == 'ubuntu-latest' && matrix.python == '3.14'"
#: The only conditions a step may have: which system it is for. Never one that can run after a failure.
RUNNER_OS_CONDITION = re.compile(r"^runner\.os (==|!=) '(Linux|macOS|Windows)'$")


def steps(job: str, path: Path = WORKFLOW) -> list[dict]:
    return workflow(path)["jobs"][job]["steps"]


def run_steps(job: str, path: Path = WORKFLOW) -> list[str]:
    return [step["run"] for step in steps(job, path) if "run" in step]


def needs(job: str) -> list[str]:
    declared = workflow()["jobs"][job].get("needs", [])
    return [declared] if isinstance(declared, str) else list(declared)


def depends_on(job: str) -> set[str]:
    """Every job that must have succeeded before `job` starts, directly or through another."""
    found: set[str] = set()
    for parent in needs(job):
        found |= {parent} | depends_on(parent)
    return found


def triggers(path: Path) -> dict:
    document = workflow(path)
    return document.get("on", document.get(True))  # YAML 1.1 reads a bare `on` as the boolean True


def actions_used(path: Path) -> list[str]:
    return [step["uses"] for job in workflow(path)["jobs"].values() for step in job.get("steps", []) if "uses" in step]


def matrix_of(job: dict) -> dict:
    return job["strategy"]["matrix"]


# -- the release workflow ------------------------------------------------------------------------------------------


def test_the_release_workflow_has_its_jobs_in_the_order_of_a_release():
    jobs = workflow()["jobs"]
    assert list(jobs) == ["check-tag", "ci", "build", "publish", "smoke-pypi", "github-release"]
    assert needs("check-tag") == []
    assert needs("ci") == ["check-tag"]
    assert sorted(needs("build")) == ["check-tag", "ci"]
    assert (
        needs("publish") == ["build"] and needs("smoke-pypi") == ["publish"] and needs("github-release") == ["publish"]
    )


def test_the_release_workflow_starts_on_a_v_tag_and_on_nothing_else():
    assert triggers(WORKFLOW) == {"push": {"tags": ["v*"]}}, "any other trigger could publish from a branch or a pr"


def test_the_release_runs_the_ci_workflow_as_a_gate_with_read_access_and_nothing_handed_over():
    call = workflow()["jobs"]["ci"]
    assert call["uses"] == CI_CALL
    assert call["permissions"] == {"contents": "read"}, "a called workflow cannot have more than its caller grants"
    assert "with" not in call and "secrets" not in call, "nothing is passed to it, not even `secrets: inherit`"
    assert "steps" not in call and "runs-on" not in call


def test_every_action_is_pinned_to_a_full_commit_sha_with_its_version_in_a_comment():
    assert {use.partition("@")[0] for use in actions_used(WORKFLOW)} == RELEASE_ACTIONS
    assert {use.partition("@")[0] for use in actions_used(CI_WORKFLOW)} == CI_ACTIONS
    for path in (WORKFLOW, CI_WORKFLOW):
        for use in actions_used(path):
            action, _, ref = use.partition("@")
            assert FULL_SHA.match(ref), f"{action} is pinned to {ref!r}, not to a 40-hex commit: a tag can be moved"
        # The comment is how a person (and Dependabot) knows what the SHA stands for.
        for line in path.read_text(encoding="utf-8").splitlines():
            if "uses:" in line and not line.lstrip().startswith("#") and CI_CALL not in line:
                assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line.strip()), (
                    f"no version comment: {line.strip()}"
                )


def test_both_workflows_pin_each_action_to_the_same_commit_so_the_ci_gate_runs_what_the_release_does():
    def pins(path: Path) -> dict[str, str]:
        return dict(use.split("@") for use in actions_used(path))

    shared = pins(CI_WORKFLOW)
    assert shared and {name: ref for name, ref in pins(WORKFLOW).items() if name in shared} == shared


def test_the_release_asks_for_nothing_by_default_and_only_publish_may_request_an_identity_token():
    document = workflow()
    assert document["permissions"] in ({}, {"contents": "read"}), "the default must be read-only"
    holders = {
        name: job.get("permissions", {}).get("id-token")
        for name, job in document["jobs"].items()
        if "permissions" in job
    }
    assert {name for name, value in holders.items() if value == "write"} == {"publish"}
    assert workflow()["jobs"]["publish"]["permissions"] == {"id-token": "write"}, "publish needs nothing else"


def test_only_the_github_release_job_can_write_to_the_repository():
    for name, job in workflow()["jobs"].items():
        contents = job.get("permissions", {}).get("contents")
        if name == "github-release":
            assert job["permissions"] == {"contents": "write"}
        else:
            assert contents in (None, "read"), f"{name} may write to the repository"


def test_publish_runs_in_the_pypi_environment_and_gives_the_publish_action_no_credential():
    job = workflow()["jobs"]["publish"]
    assert job["environment"] == {"name": "pypi", "url": "https://pypi.org/p/dynfarm"}
    publish = next(step for step in job["steps"] if step["uses"].startswith("pypa/gh-action-pypi-publish@"))
    assert "with" not in publish and "env" not in publish, "Trusted Publishing takes no password, user or token"
    for path in (WORKFLOW, CI_WORKFLOW):
        assert "secrets." not in path.read_text(encoding="utf-8"), f"{path.name} must not read a stored secret"


def test_publish_waits_for_the_tag_check_the_ci_gate_and_the_build_and_the_smoke_installs_wait_for_publish():
    assert {"check-tag", "ci", "build"} <= depends_on("publish")
    assert depends_on("github-release") >= {"check-tag", "ci", "build", "publish"}
    assert depends_on("smoke-pypi") >= {"check-tag", "ci", "build", "publish"}
    # What is published is never held back by, or made to depend on, the installs that try it afterwards.
    assert "smoke-pypi" not in depends_on("publish") | depends_on("github-release")


def test_check_tag_is_the_first_job_and_checks_the_tag_before_it_installs_or_runs_anything():
    assert needs("check-tag") == [] and next(iter(workflow()["jobs"])) == "check-tag"
    all_steps = steps("check-tag")
    first_run = next(step for step in all_steps if "run" in step)
    assert first_run["run"] == 'python tools/check_tag.py "$GITHUB_REF_NAME"', "the tag is checked before anything else"
    # What comes before it only fetches the code and a Python: nothing that could run the project's code.
    before = all_steps[: all_steps.index(first_run)]
    assert [step["uses"].partition("@")[0] for step in before] == ["actions/checkout", "actions/setup-python"]
    assert all_steps[-1] is first_run, "the job does nothing else; the suite is the ci workflow's"


def test_the_build_needs_both_the_tag_check_and_the_ci_workflow():
    assert sorted(needs("build")) == ["check-tag", "ci"]


def test_no_job_of_either_workflow_can_be_made_to_run_after_a_failed_one():
    """A condition may only choose the system a step is for; `always()` or `continue-on-error` would run past red."""
    for path in (WORKFLOW, CI_WORKFLOW):
        for name, job in workflow(path)["jobs"].items():
            assert "if" not in job and "continue-on-error" not in job, f"{path.name}: {name} could run past a red job"
            for step in job.get("steps", []):
                assert "continue-on-error" not in step, f"{path.name}: {name}"
                condition = step.get("if")
                if condition is None:
                    continue
                allowed = RUNNER_OS_CONDITION.match(condition) if path == WORKFLOW else condition == LINT_ONLY_ONCE
                assert allowed, f"{path.name}: {name}: step {step.get('name')!r} has the condition {condition!r}"
    only_smoke = [
        name for name, job in workflow()["jobs"].items() if any("if" in step for step in job.get("steps", []))
    ]
    assert only_smoke == ["smoke-pypi"]


def test_the_condition_check_can_fail():
    assert RUNNER_OS_CONDITION.match("runner.os == 'Windows'") and RUNNER_OS_CONDITION.match("runner.os != 'Windows'")
    for bad in ("always()", "failure()", "success() || failure()", "runner.os == 'Windows' || always()", "true"):
        assert not RUNNER_OS_CONDITION.match(bad), bad


def test_ci_runs_the_whole_suite_with_its_coverage_floor():
    script = "\n".join(run_steps("test", CI_WORKFLOW))
    pytest_line = next(line for line in script.splitlines() if "pytest" in line)
    assert "--cov=dynfarm" in pytest_line, (
        "the floor is fail_under in pyproject.toml; pytest-cov only reads it with --cov"
    )
    assert "tests/" not in pytest_line and "--ignore" not in pytest_line and " -k" not in pytest_line, "whole suite"
    assert "pip install -e '.[dev]'" in script
    lint = next(step for step in steps("test", CI_WORKFLOW) if "ruff" in step.get("run", ""))
    assert "ruff check ." in lint["run"] and "ruff format --check ." in lint["run"]
    assert sum("ruff" in run for run in run_steps("test", CI_WORKFLOW)) == 1


def test_ci_runs_on_main_on_call_and_by_hand_and_on_nothing_else():
    assert triggers(CI_WORKFLOW) == {"push": {"branches": ["main"]}, "workflow_call": None, "workflow_dispatch": None}
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    for other in ("pull_request", "schedule:", "tags:"):
        assert other not in text, (
            f"{other!r}: a pull request on the mirror must not run code, and a tag is the release's"
        )


def test_ci_reads_the_repository_and_nothing_else():
    document = ci_workflow()
    assert document["permissions"] == {"contents": "read"}
    for name, job in document["jobs"].items():
        assert "permissions" not in job, f"{name} asks for more than the workflow's read access"
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"] == {"persist-credentials": False}, f"{name} keeps the token in the checkout"


def test_ci_tests_the_oldest_and_newest_python_on_all_three_systems_and_does_not_stop_at_the_first_red_cell():
    job = ci_workflow()["jobs"]["test"]
    assert matrix_of(job)["os"] == THREE_SYSTEMS
    assert matrix_of(job)["python"] == ["3.11", "3.14"], "the oldest and the newest Python the package supports"
    assert job["strategy"]["fail-fast"] is False
    assert job["runs-on"] == "${{ matrix.os }}"
    assert ">=3.11" in (ROOT / "pyproject.toml").read_text(encoding="utf-8").replace(" ", "")


def test_ci_lints_in_one_cell_only():
    lint = next(step for step in steps("test", CI_WORKFLOW) if "ruff" in step.get("run", ""))
    assert lint["if"] == LINT_ONLY_ONCE


def test_ci_installs_the_built_wheel_and_sdist_on_all_three_systems_and_runs_them_as_a_person_would():
    job = ci_workflow()["jobs"]["install"]
    assert matrix_of(job)["os"] == THREE_SYSTEMS and job["strategy"]["fail-fast"] is False
    script = "\n".join(run_steps("install", CI_WORKFLOW))
    assert "python -m build" in script and "twine check dist/*" in script
    assert "python -m venv" in script and "dist/*.whl" in script and "dist/*.tar.gz" in script
    # A virtual environment keeps its scripts in `bin`, or in `Scripts` as `.exe` files on Windows.
    assert "Scripts" in script and "/bin" in script and ".exe" in script and "RUNNER_OS" in script
    for command in (
        'dynfarm$exe" --version',
        'dynfarm$exe" --help',
        'dynfarm$exe" task --help',
        'python$exe" -m dynfarm --version',
    ):
        assert command in script, command
    assert "pip check" in script


def test_ci_uses_bash_on_every_system_so_one_syntax_serves_all_three():
    assert ci_workflow()["defaults"] == {"run": {"shell": "bash"}}


def test_the_smoke_install_after_publishing_runs_on_all_three_systems_with_the_scripts_a_person_would_run():
    job = workflow()["jobs"]["smoke-pypi"]
    assert matrix_of(job)["os"] == THREE_SYSTEMS
    assert job["permissions"] == {"contents": "read"}
    named = {step["name"]: step for step in job["steps"] if "name" in step}
    wait, unix, windows = (
        named["Wait until PyPI lists the version"],
        named["Install with install.sh and run it"],
        named["Install with install.ps1 and run it"],
    )
    assert job["steps"].index(wait) < job["steps"].index(unix) < job["steps"].index(windows)
    assert wait["run"] == 'python tools/wait_for_pypi.py "${GITHUB_REF_NAME#v}"'
    assert unix["if"] == "runner.os != 'Windows'" and windows["if"] == "runner.os == 'Windows'"
    assert windows["shell"] == "pwsh" and unix["shell"] == "bash"
    assert 'sh install.sh "${GITHUB_REF_NAME#v}"' in unix["run"]
    assert "./install.ps1 -Version $version" in windows["run"]
    # What it checks is the version of the tag, on both: not "it printed something".
    assert 'test "$(dynfarm --version)" = "dynfarm ${GITHUB_REF_NAME#v}"' in unix["run"]
    assert '$shown -ne "dynfarm $version"' in windows["run"] and "throw" in windows["run"]


def test_the_wait_is_bounded_and_the_tag_is_never_pasted_into_a_script():
    assert "ATTEMPTS = 10" in (ROOT / "tools" / "wait_for_pypi.py").read_text(encoding="utf-8")
    assert "PAUSE_SECONDS = 30.0" in (ROOT / "tools" / "wait_for_pypi.py").read_text(encoding="utf-8")
    for path in (WORKFLOW, CI_WORKFLOW):
        for job in workflow(path)["jobs"].values():
            for step in job.get("steps", []):
                assert "${{ github." not in step.get("run", ""), "a ref is an input: read it from the environment"


def test_build_checks_the_metadata_and_publish_uploads_exactly_what_build_made():
    build = "\n".join(run_steps("build"))
    assert "python -m build" in build and "twine check dist/*" in build
    uploaded = next(step for step in steps("build") if step.get("uses", "").startswith("actions/upload-artifact@"))
    downloaded = next(
        step for step in steps("publish") if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert uploaded["with"]["path"] == "dist/" and downloaded["with"]["path"] == "dist/"
    assert uploaded["with"]["name"] == downloaded["with"]["name"]


def test_the_github_release_has_the_changelog_section_as_notes_and_the_built_files_attached():
    script = "\n".join(run_steps("github-release"))
    assert 'tools/check_tag.py "$GITHUB_REF_NAME" --notes release-notes.md' in script
    assert "gh release create" in script and "dist/*" in script and "--notes-file release-notes.md" in script
    assert "--verify-tag" in script, "the release must not create a tag of its own"
    gh = next(step for step in steps("github-release") if "gh release create" in step.get("run", ""))
    assert gh["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_the_workflows_never_put_a_credential_in_a_url():
    for path in (WORKFLOW, CI_WORKFLOW):
        assert CREDENTIAL_IN_URL.findall(path.read_text(encoding="utf-8")) == [], path.name


# -- the install scripts -------------------------------------------------------------------------------------------


def assignments(text: str, pattern: str) -> list[str]:
    return re.findall(pattern, text, re.MULTILINE)


def code(path: Path) -> str:
    """A script without its comments and help text: what it does, not what it says about itself."""
    text = re.sub(r"<#.*?#>", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_both_install_scripts_install_the_same_package_from_pypi_and_the_same_uv():
    sh, ps1 = INSTALL_SH.read_text(encoding="utf-8"), INSTALL_PS1.read_text(encoding="utf-8")
    for found in (
        [assignments(sh, r'^NAME="([^"]+)"'), assignments(ps1, r'^\$Name = "([^"]+)"')],
        [
            assignments(sh, r'^INSTALLER_URL="([^"]+)"'),
            [u.replace(".ps1", ".sh") for u in assignments(ps1, r'^\$UvInstallerUrl = "([^"]+)"')],
        ],
    ):
        assert found[0] and found[0] == found[1], found
    assert assignments(sh, r'^NAME="([^"]+)"') == [_brand.NAME]
    assert 'uv tool install --reinstall "$requirement"' in sh and '"$NAME==$version"' in sh
    assert '"tool", "install", "--reinstall", $Requirement' in ps1 and '"$Name==$Version"' in ps1


@pytest.mark.parametrize("path", [INSTALL_SH, INSTALL_PS1], ids=lambda p: p.name)
def test_the_install_scripts_handle_no_token_no_credential_and_no_other_package_source(path):
    forbidden = re.compile(
        r"token|password|passwd|secret|credential|username|private-token|authorization|--index|--extra-index|"
        r"index-url|UV_INDEX|\bgit\b|git\+",
        re.IGNORECASE,
    )
    assert forbidden.findall(code(path)) == [], f"{path.name} handles a credential or installs from somewhere else"


@pytest.mark.parametrize(
    "path",
    [INSTALL_SH, INSTALL_PS1, WORKFLOW, CI_WORKFLOW, ROOT / ".gitlab-ci.yml", ROOT / "README.md"],
    ids=lambda p: p.name,
)
def test_no_install_path_puts_a_credential_in_a_url(path):
    assert CREDENTIAL_IN_URL.findall(path.read_text(encoding="utf-8")) == []


def test_install_ps1_never_exits_the_callers_terminal_and_checks_what_it_runs():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert re.search(r"^\s*exit\b", code(INSTALL_PS1), re.MULTILINE) is None  # `throw`: it is often run in a terminal
    assert '$ErrorActionPreference = "Stop"' in text
    assert "[string]$Version" in text and "--reinstall" in text
    assert text.count("{") == text.count("}") and text.count("(") == text.count(")")  # a missing brace is not a script
    assert "'$Version' is not a version like 0.1.0" in text


def shell() -> str:
    if sys.platform == "win32":
        pytest.skip(
            "install.sh is for macOS and Linux; Windows has install.ps1 (a Git Bash `sh` is not what it targets)"
        )
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX sh on this machine (Windows): install.ps1 is its installer")
    return found


def test_install_sh_is_valid_posix_shell():
    run = subprocess.run([shell(), "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert INSTALL_SH.read_text(encoding="utf-8").startswith("#!/bin/sh\n")


class Machine:
    """A machine where `uv` and `curl` are scripts that write down what they were asked, and nothing else exists.

    The PATH holds only these and symlinks to the few system tools install.sh uses, so a real `uv` on the developer's
    machine cannot answer instead of the fake one, and a `git` would not be found: the script must not need it.
    """

    TOOLS = ("sh", "sed", "grep", "mktemp", "rm", "cat", "mkdir", "cp", "chmod", "env", "tr")

    def __init__(self, tmp_path: Path, *, with_uv: bool = True) -> None:
        shell()  # skips on Windows before anything is built: the fake tools are symlinks to POSIX programs
        self.home = tmp_path / "home"
        self.bin = tmp_path / "fakebin"
        self.system = tmp_path / "system"
        self.tool_bin = tmp_path / "toolbin"  # where `uv tool dir --bin` says tools go; not on PATH
        self.log = tmp_path / "calls.log"
        for directory in (self.home, self.bin, self.system, self.tool_bin):
            directory.mkdir()
        self.log.write_text("", encoding="utf-8")
        for tool in self.TOOLS:
            (self.system / tool).symlink_to(shutil.which(tool))
        self.uv_script = self._script(
            "uv-source",
            """
            echo "uv $*" >> "$FAKE_LOG"
            if [ "$1 $2 $3" = "tool dir --bin" ]; then echo "$FAKE_TOOL_BIN"; fi
            [ -z "${FAKE_UV_FAILS:-}" ] || exit 1
            """,
            install=False,
        )
        if with_uv:
            shutil.copy(self.uv_script, self.bin / "uv")
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
        self._script("dynfarm", 'echo "dynfarm 0.1.0"', directory=self.tool_bin)

    def _script(self, name: str, body: str, *, install: bool = True, directory: Path | None = None) -> Path:
        directory = directory or (self.bin if install else self.bin.parent)
        path = directory / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body).strip("\n") + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def run(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{self.system}",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_TOOL_BIN": str(self.tool_bin),
            "FAKE_UV_SOURCE": str(self.uv_script),
            **(extra_env or {}),
        }
        return subprocess.run([shell(), str(INSTALL_SH), *args], capture_output=True, text=True, env=env, check=False)

    @property
    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    @property
    def uv_calls(self) -> list[str]:
        return [line for line in self.calls if line.startswith("uv ")]


def test_without_a_version_it_installs_the_latest_dynfarm_from_pypi_and_runs_the_check(tmp_path):
    machine = Machine(tmp_path)
    run = machine.run()
    assert run.returncode == 0, run.stderr
    assert machine.uv_calls[0] == "uv tool install --reinstall dynfarm"
    assert "from PyPI" in run.stdout
    assert run.stdout.strip().splitlines()[-3:-1] == ["dynfarm 0.1.0", ""]  # the check ran the installed command


@pytest.mark.parametrize("given", ["0.1.0", "v0.1.0"])
def test_a_version_with_or_without_the_v_pins_the_requirement(tmp_path, given):
    machine = Machine(tmp_path)
    assert machine.run(given).returncode == 0
    assert machine.uv_calls[0] == "uv tool install --reinstall dynfarm==0.1.0"


def test_a_token_in_the_environment_does_not_change_what_is_installed_or_where_from(tmp_path):
    machine = Machine(tmp_path)
    run = machine.run("0.1.0", extra_env={"DYNFARM_TOKEN": FAKE_TOKEN, "UV_INDEX_DYNFARM_PASSWORD": FAKE_TOKEN})
    assert run.returncode == 0, run.stderr
    assert machine.uv_calls == ["uv tool install --reinstall dynfarm==0.1.0", "uv tool dir --bin"]
    assert FAKE_TOKEN not in machine.log.read_text(encoding="utf-8") + run.stdout + run.stderr
    assert not any(call.startswith("git ") for call in machine.calls), (
        "there is no git on this machine, and none is used"
    )


@pytest.mark.parametrize("bad", ["x;rm -rf ~", "$(id)", "0.1.0 --index evil=http://x", "v", "latest", "../0.1"])
def test_a_version_that_is_not_a_version_is_refused_before_anything_runs(tmp_path, bad):
    machine = Machine(tmp_path)
    run = machine.run(bad)
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
    assert run.returncode == 0 and "Usage: sh install.sh [VERSION]" in run.stdout and "PyPI" in run.stdout
    assert machine.calls == []


def test_a_missing_uv_is_installed_from_astral_first_and_then_used(tmp_path):
    machine = Machine(tmp_path, with_uv=False)
    run = machine.run()
    assert run.returncode == 0, run.stderr
    curl = next(call for call in machine.calls if call.startswith("curl "))
    assert "https://astral.sh/uv/install.sh" in curl and "-o " in curl  # to a file first, never `| sh`
    assert (machine.home / ".local" / "bin" / "uv").exists()
    assert machine.uv_calls and machine.uv_calls[0].startswith("uv tool install")


def test_a_failed_uv_download_stops_the_install_and_says_so(tmp_path):
    machine = Machine(tmp_path, with_uv=False)
    run = machine.run(extra_env={"FAKE_CURL_FAILS": "1"})
    assert run.returncode == 1 and "uv installer" in run.stderr
    assert machine.uv_calls == []


def test_a_failed_uv_install_is_reported_and_is_not_followed_by_a_success_message(tmp_path):
    machine = Machine(tmp_path)
    run = machine.run("9.9.9", extra_env={"FAKE_UV_FAILS": "1"})
    assert run.returncode == 1 and "dynfarm==9.9.9 from PyPI" in run.stderr
    assert "Next:" not in run.stdout


def test_when_the_tool_directory_is_not_on_the_path_it_says_how_to_fix_it_and_still_runs_the_check(tmp_path):
    machine = Machine(tmp_path)
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
    assert "Project-URL: Source, https://github.com/dynapps-es/dynfarm" in metadata and PRIVATE_NAMES[0] not in metadata


# -- the documentation ---------------------------------------------------------------------------------------------


RAW = "https://raw.githubusercontent.com/dynapps-es/dynfarm/main"


def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


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
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "dynfarm").rglob("*.py"))
    names = {_brand.env(suffix) for suffix in re.findall(r'\benv\("([A-Z_]+)"\)', sources)}
    names |= {"NO_COLOR", "CI"}
    assert {"DYNFARM_HOST", "DYNFARM_TOKEN", "DYNFARM_CONFIG_DIR", "DYNFARM_DEBUG"} <= names
    section = readme().split("### Environment variables", 1)[1].split("###", 1)[0]
    missing = sorted(name for name in names if f"`{name}`" not in section)
    assert missing == [], f"not in the README's environment table: {missing}"


def test_the_readme_has_the_way_in_the_way_out_and_the_ci_recipe():
    text = readme()
    for needle in (
        "uv tool install dynfarm",
        "pipx install dynfarm",
        "pip install dynfarm",
        "py -m pip install dynfarm",
        "py -m dynfarm --version",
        f"curl -LsSf {RAW}/install.sh | sh",
        'powershell -ExecutionPolicy ByPass -c "irm ' + RAW + '/install.ps1 | iex"',
        "uv tool upgrade dynfarm",
        "## First steps",
        "### Several farms",
        "## Upgrade and uninstall",
        "uv tool uninstall dynfarm",
        "dynfarm completion",
        "DYNFARM_HOST",
        "DYNFARM_TOKEN",
        "masked",
        "--exit-status",
    ):
        assert needle in text, needle


def test_the_readme_is_a_page_pypi_can_render_so_its_links_are_absolute():
    """PyPI shows the README outside the repository: a relative link such as `CHANGELOG.md` is a 404 there."""
    links = re.findall(r"\]\(([^)\s]+)\)", readme())
    assert links, "the README has no links at all: the pattern is not reading it"
    assert [link for link in links if not link.startswith(("https://", "#"))] == []


# -- nothing public points inside --------------------------------------------------------------------------------

#: What must not appear anywhere in the repository, because the whole repository is mirrored to a public site: the
#: private GitLab host, the internal domain of the farms, the keychain item of an internal client and that client's
#: file name. Each is written in two pieces so that this file does not contain what it forbids.
PRIVATE_NAMES = ("gitlab" + ".dynapps.be", "dyncloud" + ".es", "agent-farm" + "-token", "farm" + ".py")

#: The one place a private name is allowed, and why: the pinned copy of the farm's own OpenAPI document keeps the
#: git host that the farm's documentation uses as an example. It is the farm's text, refreshed byte for byte from the
#: API (`tools/fetch_openapi.py`), so editing it here would be undone by the next refresh and would make the parity
#: test compare a document that is not the API's. The other three names are still checked in it.
ALLOWED_PRIVATE_NAMES = {("tests/fixtures/openapi.json", PRIVATE_NAMES[0])}


def tracked_files() -> list[str]:
    """Files git would publish: tracked, plus new ones not ignored (so a file added in this change is checked before
    it is committed). Skips when this is not a git checkout (an unpacked sdist)."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: nothing to list")
    run = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return [name for name in run.stdout.splitlines() if (ROOT / name).is_file()]


def private_names_in(name: str, text: str) -> list[str]:
    return [found for found in PRIVATE_NAMES if found in text and (name, found) not in ALLOWED_PRIVATE_NAMES]


def test_no_file_of_the_repository_names_a_private_host_or_an_internal_client():
    names = tracked_files()
    assert len(names) > 50 and "README.md" in names, "the listing is not the repository"
    found = {}
    for name in names:
        text = (ROOT / name).read_text(encoding="utf-8", errors="ignore")
        if hits := private_names_in(name, text):
            found[name] = hits
    assert found == {}, f"private names in files that will be public: {found}"


def test_the_private_names_scan_can_fail_and_its_one_exemption_is_narrow():
    for private in PRIVATE_NAMES:
        assert private_names_in("README.md", f"see {private} for more") == [private]
    exempt, host = next(iter(ALLOWED_PRIVATE_NAMES))
    assert private_names_in(exempt, f"https://{host}/x") == []
    assert private_names_in(exempt, f"{PRIVATE_NAMES[1]} and {PRIVATE_NAMES[2]}") == [
        PRIVATE_NAMES[1],
        PRIVATE_NAMES[2],
    ]
    assert private_names_in("src/dynfarm/cli.py", f"https://{host}/x") == [host]
    # The exemption is real: the fixture does carry the host, so removing the entry would turn the scan red.
    assert host in (ROOT / exempt).read_text(encoding="utf-8")


def test_the_project_links_on_pypi_point_to_the_github_mirror():
    urls = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["urls"]
    assert urls == {
        "Homepage": "https://github.com/dynapps-es/dynfarm",
        "Source": "https://github.com/dynapps-es/dynfarm",
        "Changelog": "https://github.com/dynapps-es/dynfarm/blob/main/CHANGELOG.md",
        "Issues": "https://github.com/dynapps-es/dynfarm/issues",
    }
    assert tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["keywords"]


def test_the_code_and_the_docs_carry_no_reference_to_the_plan_that_built_them():
    scanned = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "install.sh", "install.ps1")]
    scanned += sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))
    scanned += sorted((ROOT / "tools").glob("*.py")) + [ROOT / ".gitlab-ci.yml", WORKFLOW, CI_WORKFLOW]
    plan_reference = re.compile(r"\b(?:D\d{1,2}|[Pp]hase \d+|research Q\d)\b")
    this_file = Path(__file__).resolve()
    for path in scanned:
        if path.resolve() == this_file:
            continue  # it holds the pattern it looks for
        text = path.read_text(encoding="utf-8")
        assert not plan_reference.search(text), f"{path.relative_to(ROOT)} refers to a plan phase or decision"
