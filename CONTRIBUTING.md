# Contributing to dynfarm

`dynfarm` is developed in a private GitLab project of Dynapps (merge requests, the CI described below, protected
branches and tags). The public GitHub repository, [dynapps-es/dynfarm](https://github.com/dynapps-es/dynfarm), is a
**read-only mirror**: a push mirror copies `main` and the tags there, and nothing is ever pushed to it by hand, so a
pull request opened on GitHub cannot be merged there. Outside contributors: open an issue on GitHub; the maintainers
carry a change over to the private project. PyPI and the GitHub Releases are fed from the mirror (see "Releasing").

## Setting up

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # the whole suite
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Repeat the `pip install` after every pull: dependencies are pinned and change with the code.

On Windows the same, with `py -m venv .venv` and `.venv\Scripts\pip install -e ".[dev]"`; the commands are
`.venv\Scripts\python -m pytest` and so on. The package runs on Linux, macOS and Windows, and code and tests are written
for all three (see "Portable code and tests").

## Conventions

- **Everything in English**: code, comments, docstrings, commits, docs.
- **Dependencies pinned to an exact version** in `pyproject.toml`, the single source. What CI tested is what a person
  installs. Bump a pin in its own commit, with the reason.
- **Configuration fails closed.** A value with no safe default stops the command with a clear message and exit 2;
  never guess a farm, never fall back to plain `http` for a remote host.
- **A token or a secret never reaches** stdout, stderr, a log line, an exception message, a URL, a command line or a
  file other than the token store. Tests assert it.
- **Tests are named as behaviour sentences** (`test_login_does_not_save_a_token_the_farm_rejects`), each module opens
  with a docstring that says *why* it exists, and assertions check behaviour, not implementation.
- **Docs travel with the change**, in the same commit: `README.md` for users, this file for contributors,
  `CHANGELOG.md` for releases.
- **Commits**: conventional commits with a scope — `feat(cli): …`, `fix(auth): …`, `test: …`, `ci: …`, `docs: …`.
  Branches `feat/…`, `fix/…`, `docs/…`; merge requests with a merge commit, opened as drafts.
- **The name `dynfarm` lives in `src/dynfarm/_brand.py`.** Never hard-code it elsewhere; derive env names, paths and
  the keyring service from `_brand`.
- **Every environment variable of the tool starts with `DYNFARM_`** (`_brand.env("NAME")` builds the name). Variables
  of other tools are not read, not even as an alias — least of all a token variable: a token
  exported for one farm in a shell profile is exactly what must not follow a person to another. The only
  others the code and the install scripts may mention are standard conventions (`CI`, `NO_COLOR`, `TERM`, `HOME`,
  `PATH`, `XDG_*`, the proxy variables httpx reads), each listed with its reason in an allowlist in
  `tests/test_env_prefix.py`, which scans `src/` and both install scripts. A new variable outside the prefix fails
  that test; adding it to the allowlist needs a reason a reviewer can check.

## Tests

`tools/expect_passed.py MIN` is the gate used when running one file: `pytest tests/test_x.py -q | python3
tools/expect_passed.py 10` fails unless at least 10 tests passed and none failed.

`tests/fixtures/openapi.json` is the Agent Farm API this version of `dynfarm` was written against (a
development farm's, pretty-printed, sorted keys). Refresh it when the API changes, after the CLI has been updated to match
(`tests/test_parity.py` says what to update):

```bash
python3 tools/fetch_openapi.py https://farm.example.com/openapi.json tests/fixtures/openapi.json --pretty
git diff tests/fixtures/openapi.json      # the API's change and nothing else
```

The fake farm validates every request and answer against that file, so a refresh can also turn command tests red:
that is the CLI being told about the new contract.

## CI

There are two CIs, because the project lives in two places. In the private GitLab project (**Linux runners only**) every
push runs `lint`, `test`, `e2e` and `package`, and a daily schedule on `main` runs `api-drift`. On the public GitHub
mirror, `.github/workflows/ci.yml` runs the suite on **Linux, Windows and macOS** (see "CI on the mirror" below). Each
job is a command you can run locally; the pipeline is red when any of them is.

| Job | Guards against | Locally |
|---|---|---|
| `lint` | style drift, unused imports, unformatted code | `ruff check . && ruff format --check .` |
| `test` (Python 3.11–3.14) | a behaviour that breaks on one supported Python; untested code (coverage floor) | `pytest --ignore=tests/e2e --cov=dynfarm` |
| `e2e` | a broken entry point, exit code, `--json`, token or secret handling in the *installed* package | `pip install '.[dev]'` (not `-e`), then `pytest tests/e2e tests/test_secret_leaks.py` |
| `package` | a wheel or sdist that does not install or run in a clean environment, or whose metadata PyPI would refuse | `python -m build && twine check dist/*`, then install `dist/*.whl` in a new venv and run `dynfarm --help` |
| `api-drift` | the API changed under the CLI: a new operation without a command, a changed enum, a new required field, a removed field | `python3 tools/fetch_openapi.py URL live.json && DYNFARM_PARITY_OPENAPI=live.json pytest tests/test_parity.py` |

- **Coverage floor**: `fail_under` in `pyproject.toml` (`[tool.coverage.report]`), measured with branches. It is the
  measured figure rounded down to the nearest 5 and never below 85. Raise it when the measure rises by 5; never lower
  it to get a change through, write the missing test. A single test file run locally has no floor on purpose.
- **`tests/test_parity.py`** is the gate between the CLI and the API. Every `operationId` needs a command that declares
  it with `@operation(...)` (`dynfarm.registry`) or an entry in `EXCLUDED` with the reason. Every fixed list of values
  a command offers (`click.Choice`) must be mapped to the API enum it mirrors, or declared as not the API's. Each check
  is a function that returns its problems, and the file proves each one can fail on a mutated copy of the spec.
- **`tests/e2e/`** runs the `dynfarm` that `pip install` put next to the test interpreter (a missing one is a failure,
  not a skip), in a subprocess, against `fake_farm.serve()` on a real socket, with its own `HOME` and config dir, `CI=1`
  (so no keyring is touched) and `NO_PROXY`. One journey per resource, the exit codes a script branches on (0, 1, 2, 4),
  the retry pauses, and `--help` / `--json` for every command read from the command tree, so a new command is covered
  the day it is added. When a command needs a task to change state while it is watched, the `finish_after_creation`
  fixture walks it through states. The editable install also puts a `dynfarm` next to the interpreter, so the suite
  runs locally without a second install; use a throw-away venv for the non-editable one, or reinstall with `-e` after.
- **`api-drift` failed**: read the job log; it lists what drifted. If the CLI must change, do it, then refresh the
  fixture (above). If the API change is not meant to be supported, add an `EXCLUDED` entry with the reason. The job also
  fails when the runner cannot download the document: that is the farm being unreachable, not drift. The farm's address is the CI/CD variable `FARM_OPENAPI_URL` of the project (the
  URL of its `/openapi.json`, which needs no token); the job fails saying so when it is not set. The job can be
  started by hand from any pipeline.
- **`check-tag` runs only on a tag `v*`**: the tag must be the package version and the changelog must have its section
  (`tools/check_tag.py`), so a bad tag is red on GitLab too. Nothing is published from GitLab: publishing is the job of
  `.github/workflows/release.yml` on the mirror (see "Releasing").
- **The live smoke test against a real farm is not in CI**: CI has no farm token and must not get one.

### CI on the mirror

GitLab has no Windows or macOS runners, GitHub offers both for free, so the mirror is where the code runs on them.
`.github/workflows/ci.yml` starts on a push to `main` (what the push mirror delivers), by hand (Actions, "Run
workflow") and when `release.yml` calls it (`workflow_call`). Nothing else starts it: not a pull request, since the
mirror takes none, and not a tag, which is the release's. It reads the repository and has no secret.

| Job | What it runs | Cells |
|---|---|---|
| `test` | `pip install -e '.[dev]'`, then the whole suite with the coverage floor (`pytest --cov=dynfarm`); `ruff` only in one cell | Linux, Windows, macOS x Python 3.11 and 3.14 |
| `install` | `python -m build`, then the wheel and the sdist each in a fresh virtual environment: `dynfarm --version`, `--help`, `task --help`, `python -m dynfarm --version` | Linux, Windows, macOS (Python 3.14) |

A red cell on one system does not cancel the others (`fail-fast: false`): the first run on a system tells the most. The
workflow's first run on `main` is the first time the code runs on Windows and macOS; a failure there is a portability
bug to fix in the private project and carry over, never a reason to edit the mirror. Reproduce a Windows failure from
the job log; its cell names the operating system and Python.

The Windows cells run `bash` (Git Bash, preinstalled), which is why `ci.yml` sets it as the default shell: one syntax for
the three systems. A few tests are skipped on Windows, each with its reason in the `-ra` summary: file modes
(`0600`/`0700`), pseudo-terminals, and the tests of `install.sh` (Windows has `install.ps1`).

### Portable code and tests

Rules that keep the three systems green; `tests/test_portability.py` enforces what can be enforced.

- **Say the encoding of every text file** (`read_text(encoding="utf-8")`, `write_text(..., encoding="utf-8")`): the
  default of a Windows machine is a legacy code page and `\n` becomes `\r\n`. Where the exact bytes matter (a secret in a
  file, a line ending under test), use `write_bytes`. The portability test scans `src`, `tests` and `tools` for a call that
  forgets.
- **POSIX-only things are guarded**: `os.chmod` and `os.fchmod` run only when `os.name == "posix"` (a Windows Python has
  no `fchmod`), no `fcntl`, `termios` or `pty` import at the top of a module in `src`, and a test that asserts a file
  mode or needs a pseudo-terminal is skipped on Windows with a reason, never silently.
- **Find the installed script with `tests/childproc.py`** (`installed_binary()`, `child_environment()`), not with a
  path such as `.venv/bin/dynfarm`: Windows has `Scripts\dynfarm.exe`, and a plain Python there keeps its scripts next
  to, not in, the interpreter's directory. A child's scrubbed environment keeps `SYSTEMROOT` and sets `TEMP`, or Windows
  cannot start Python with sockets.
- **Do not assert on a sleep to the millisecond**: a Windows clock ticks every 15 ms.
- **`.gitattributes` makes every text file LF** on checkout. Do not add a file that needs CRLF.

## Testing a command with the fake farm

`tests/fakefarm/` is an in-memory model of the whole API. It checks every request against the pinned OpenAPI and every
answer too, so a command that sends a field the API does not know fails its test even if the test never looks.

- Fixtures (`tests/conftest.py`): `fake_farm` (the model), `run_cli(args, input=None, token=...)` (the real `dynfarm`
  in-process, against the fake, as `tok-alice` by default) and `sleeps` (the retry pauses, recorded instead of slept).
  The environment is scrubbed of `DYNFARM_*` and the config dir is a temp dir in every test.
- The keyring is an in-memory one in every test (`memory_keyring`, autouse; `memory_keyring.entries` and `.calls`),
  never the real Keychain, which would pop dialogs and keep test tokens. `use_keyring(fail.Keyring())` swaps in another
  backend (absent, broken, forgetful). `CI` is unset in every test; set it only to test what it changes.
- Tokens: `ALICE`, `BOB` (same tenant), `OTHER` (another tenant), `ADMIN` (`farm:tenant:admin`), `READONLY`, `EXPIRED`.
  Another person's or tenant's object is a 404, as in the real farm.
- Arrange through the model (`fake_farm.seed_task(ALICE, state="running")`, `set_task`, `set_schedule`,
  `fail_next("get_task", ...)`)
  or through the API, then `result = run_cli(["task", "view", id])` and assert on `result.exit_code`, `result.stdout`,
  `result.stderr` and on `fake_farm.requests` (method, path, headers, body of everything that crossed the wire).
- When a test needs an answer the model never gives (a field the farm may add, a task that moves through states while
  it is watched, an empty list), `fake_farm.override("get_task", handler)` replaces one operation's handler
  (`handler(call) -> (status, body)`) and returns the one it replaced, so it can wrap it. The answer is still checked
  against the OpenAPI. Never reach into `fake_farm._handlers`.
- A test that sends something invalid on purpose asserts `fake_farm.take_violations()`; any other leftover violation
  fails the test at teardown. For a real socket (the installed binary) use `with fake_farm.serve() as url:`.
- Declare the API operations a command calls with `@operation("list_tasks")` (`dynfarm.registry`); the parity test reads it.
- `tests/test_secret_leaks.py` runs the installed `dynfarm` (the script `pip install -e` puts next to the test's
  python; the test fails, it does not skip, when it is missing) in a subprocess against `fake_farm.serve()`, with a
  scrubbed environment (`CI=1`, a failing keyring backend, its own `HOME` and config dir, `NO_PROXY`). Any new way
  for a secret to enter or leave the tool gets a test there: the sentinel must be in the body of `POST /credentials`
  and in nothing else — output, `--debug`, files, other requests, argv.

## Releasing

A release is a tag `v<version>` on the default branch, and it is a maintainers' decision: the tag is the act that
publishes, so nobody cuts one to "see what happens". `v*` tags are protected in the GitLab project.

1. In one commit on a branch: set `__version__` in `src/dynfarm/__init__.py` (the only place; `pyproject.toml` reads it)
   and turn the `## [x.y.z] - unreleased` heading of `CHANGELOG.md` into `## [x.y.z] - YYYY-MM-DD`. Write what a person
   upgrading needs to know, because that section is the text of the GitHub Release.
2. Open the merge request, get a green pipeline, and merge it.
3. Create the protected tag `vx.y.z` on the merge commit in GitLab (Code, Tags) and push it. The tag pipeline runs
   `lint`, `test`, `e2e`, `package` and `check-tag`; a red `check-tag` says the tag and the version or changelog
   disagree: delete the tag, fix, tag again.
4. The push mirror copies `main` and the tag to GitHub. The tag starts `.github/workflows/release.yml`, which runs, in
   order:
   - `check-tag`: `tools/check_tag.py` alone, first, so a bad tag fails in seconds;
   - `ci`: the whole of `ci.yml` (lint, the suite on Linux, Windows and macOS, the install of the built package on each).
     **A red cell on any system stops the release**: nothing is published from a tree that fails where people install it;
   - `build`: the sdist and the wheel, `twine check`. It needs `check-tag` and `ci`;
   - `publish`: uploads them to PyPI with **Trusted Publishing** (OIDC, no token stored anywhere) in the `pypi` GitHub
     environment, restricted to `v*` tags. It is the only job that may request an identity token;
   - `smoke-pypi`: on Linux, Windows and macOS, waits (at most 10 x 30 s, `tools/wait_for_pypi.py`) until PyPI lists the
     version, runs `install.sh` or `install.ps1` with it and checks that `dynfarm --version` prints the tag's version. It
     cannot unpublish: if it is red, the release is out but does not install on that system, and the fix is the next
     patch version;
   - `github-release`: creates the GitHub Release with the changelog section as notes and the files attached. It runs
     as soon as `publish` succeeds, in parallel with `smoke-pypi`.
5. Read the `smoke-pypi` cells; they are the install on a clean machine of each system. By hand, the same is
   `uv tool install dynfarm==x.y.z` (or `sh install.sh x.y.z`) and `dynfarm --version`.

**One-time setup** (done once, by an owner of the PyPI project and of the GitHub repository): on PyPI, add a *pending*
trusted publisher for the project `dynfarm` (Account settings, Publishing) with owner `dynapps-es`, repository
`dynfarm`, workflow `release.yml` and environment `pypi`; the first successful run creates the project. In GitHub,
the environment `pypi` exists and allows only `v*` tags. In GitLab, the push mirror to the repository above covers
`main` and the tags.

PyPI accepts a version once. If `publish` failed after uploading, do not re-run it: fix forward with the next patch
version. A bad release is not deleted; it is superseded, and yanked on PyPI when installers must not pick
it any more (a yanked version stays installable by exact version).

`tools/check_tag.py vX.Y.Z` can be run locally before tagging: it says what would be refused. The install scripts, the
workflows and `tests/test_packaging.py` change together: the test pins what the scripts may and may not do (install
`dynfarm` from PyPI, a version they refuse to pass on to a shell, no credential anywhere) and what the workflows may
(`release.yml` triggered only by `v*` tags and `ci.yml` only by `main`, a call and a manual run; every action pinned to a
commit SHA, the same one in both; `id-token: write` in `publish` alone; `ci.yml` read-only; the order of the jobs, with
`build` after the tag check and the CI gate and `smoke-pypi` after `publish`). When you bump an action, look the commit up (`gh api repos/OWNER/ACTION/commits/TAG --jq .sha`) and keep the
version in the comment next to it. The shell script is tested with a fake `uv` and `curl`; `install.ps1` is checked statically and,
after a release, run for real by `smoke-pypi` on a Windows runner, so a change to it is only proven by a tag: try it on a
Windows machine first.

## Live smoke

`tools/smoke_live.sh FARM_URL` drives an installed `dynfarm` against a real farm with a real token, through the real
system keyring and a temporary config dir: login, status, whoami, the read commands, a far-future schedule created,
paused and deleted, a throw-away credential set and deleted, logout. It cleans up what it created even when a step
fails, and launches no task. Install your checkout in a fresh venv and give the script the token by environment (a
variable you set from wherever you keep the token, never typed on a command line):

```bash
python3 -m venv /tmp/dynfarm-smoke && /tmp/dynfarm-smoke/bin/pip install -q .       # or "git+file://$PWD@BRANCH"
DYNFARM_SMOKE_TOKEN=... DYNFARM_SMOKE_VALUE="smoke-$(openssl rand -hex 12)" PATH="/tmp/dynfarm-smoke/bin:$PATH" \
  bash tools/smoke_live.sh https://farm.example.com
```
