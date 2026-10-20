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
pretty-printed, sorted keys). Refresh it when the API changes:

```bash
curl -sf https://farm-dev.ai.dyncloud.es/openapi.json \
  | python3 -c "import json,sys;json.dump(json.load(sys.stdin),sys.stdout,indent=2,sort_keys=True);print()" \
  > tests/fixtures/openapi.json
```

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
- Arrange through the model (`fake_farm.seed_task(ALICE, state="running")`, `set_task`, `fail_next("get_task", ...)`)
  or through the API, then `result = run_cli(["task", "view", id])` and assert on `result.exit_code`, `result.stdout`,
  `result.stderr` and on `fake_farm.requests` (method, path, headers, body of everything that crossed the wire).
- When a test needs an answer the model never gives (a field the farm may add, a task that moves through states while
  it is watched, an empty list), `fake_farm.override("get_task", handler)` replaces one operation's handler
  (`handler(call) -> (status, body)`) and returns the one it replaced, so it can wrap it. The answer is still checked
  against the OpenAPI. Never reach into `fake_farm._handlers`.
- A test that sends something invalid on purpose asserts `fake_farm.take_violations()`; any other leftover violation
  fails the test at teardown. For a real socket (the installed binary) use `with fake_farm.serve() as url:`.
- Declare the API operations a command calls with `@operation("list_tasks")` (`dynfarm.registry`); the parity test reads it.
