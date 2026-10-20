# Contributing to dynfarm

## Setting up

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # the whole suite
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Repeat the `pip install` after every pull: dependencies are pinned and change with the code.

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

## Tests

`tools/expect_passed.py MIN` is the gate used when running one file: `pytest tests/test_x.py -q | python3
tools/expect_passed.py 10` fails unless at least 10 tests passed and none failed.

`tests/fixtures/openapi.json` is the Agent Farm API this version of `dynfarm` was written against (es-dev-01,
pretty-printed, sorted keys). Refresh it when the API changes, after the CLI has been updated to match
(`tests/test_parity.py` says what to update):

```bash
python3 tools/fetch_openapi.py https://farm-dev.ai.dyncloud.es/openapi.json tests/fixtures/openapi.json --pretty
git diff tests/fixtures/openapi.json      # the API's change and nothing else
```

The fake farm validates every request and answer against that file, so a refresh can also turn command tests red:
that is the CLI being told about the new contract.

## CI

Every push runs `lint`, `test`, `e2e` and `package`; a daily schedule on `main` runs `api-drift`. Each job is a command
you can run locally; the pipeline is red when any of them is.

| Job | Guards against | Locally |
|---|---|---|
| `lint` | style drift, unused imports, unformatted code | `ruff check . && ruff format --check .` |
| `test` (Python 3.11–3.14) | a behaviour that breaks on one supported Python; untested code (coverage floor) | `pytest --ignore=tests/e2e --cov=dynfarm` |
| `e2e` | a broken entry point, exit code, `--json`, token or secret handling in the *installed* package | `pip install '.[dev]'` (not `-e`), then `pytest tests/e2e tests/test_secret_leaks.py` |
| `package` | a wheel or sdist that does not install or run in a clean environment, or whose metadata the registry would refuse | `python -m build && twine check dist/*`, then install `dist/*.whl` in a new venv and run `dynfarm --help` |
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
  fails when the runner cannot download the document: that is the farm being unreachable, not drift. The job can be
  started by hand from any pipeline.
- **`publish` and `release` run only on a tag `v*`**, after every job above has passed on it (see "Releasing").
- **The live smoke test against a real farm is not in CI**: CI has no farm token and must not get one.

## Testing a command with the fake farm

`tests/fakefarm/` is an in-memory model of the whole API. It checks every request against the pinned OpenAPI and every
answer too, so a command that sends a field the API does not know fails its test even if the test never looks.

- Fixtures (`tests/conftest.py`): `fake_farm` (the model), `run_cli(args, input=None, token=...)` (the real `dynfarm`
  in-process, against the fake, as `tok-alice` by default) and `sleeps` (the retry pauses, recorded instead of slept).
  The environment is scrubbed of `DYNFARM_*` / `AGENT_FARM_*` and the config dir is a temp dir in every test.
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
publishes, so nobody cuts one to "see what happens". `v*` tags are protected in the project.

1. In one commit on a branch: set `__version__` in `src/dynfarm/__init__.py` (the only place; `pyproject.toml` reads it)
   and turn the `## [x.y.z] - unreleased` heading of `CHANGELOG.md` into `## [x.y.z] - YYYY-MM-DD`. Write what a person
   upgrading needs to know, because that section is the text of the GitLab Release.
2. Merge it, with a green pipeline.
3. Tag the merge commit `vx.y.z` and push the tag. The tag pipeline runs `lint`, `test`, `e2e` and `package` again, then:
   - `publish` runs `tools/check_tag.py`, which fails when the tag is not exactly `v` + the package version or when the
     changelog has no section for it; builds the sdist and the wheel; runs `twine check`; and uploads both to the
     project's package registry with the job token (`TWINE_USERNAME=gitlab-ci-token`, `TWINE_PASSWORD=$CI_JOB_TOKEN`),
     so no secret is stored for it;
   - `release` creates the GitLab Release with the changelog section as its notes and links to the wheel, the sdist and
     the two install scripts at that tag.
4. Try the install on a clean machine: `sh install.sh x.y.z` (or `.\install.ps1 -Version x.y.z`) and `dynfarm --version`.

The registry accepts a version once. If `publish` failed after uploading, do not re-run it: fix forward with the next
patch version. A bad release is not deleted; it is superseded.

`tools/check_tag.py vX.Y.Z` can be run locally before tagging: it says what would be refused. The install scripts and
`tests/test_packaging.py` change together: the test pins what the scripts may and may not do (no token in a URL, the
same registry, git address and index name in both, a version they refuse to pass on to a shell). The scripts are
tested with a fake `uv`, `git` and `curl` for the shell one; `install.ps1` has no runner in CI, so it is checked
statically, and a change to it needs a run on a real Windows machine before the tag.

## Live smoke

`tools/smoke_live.sh FARM_URL` drives an installed `dynfarm` against a real farm with a real token, through the real
system keyring and a temporary config dir: login, status, whoami, the read commands, a far-future schedule created,
paused and deleted, a throw-away credential set and deleted, logout. It cleans up what it created even when a step
fails, and launches no task. Install the branch in a fresh venv and give it the token by environment, never on the
command line:

```bash
python3 -m venv /tmp/dynfarm-smoke && /tmp/dynfarm-smoke/bin/pip install -q "git+file://$PWD@feat/initial-cli"
DYNFARM_SMOKE_TOKEN="$(security find-generic-password -s agent-farm-token -w)" \
  DYNFARM_SMOKE_VALUE="smoke-$(openssl rand -hex 12)" PATH="/tmp/dynfarm-smoke/bin:$PATH" \
  bash tools/smoke_live.sh https://farm-dev.ai.dyncloud.es
```
