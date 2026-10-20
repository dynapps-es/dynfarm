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
