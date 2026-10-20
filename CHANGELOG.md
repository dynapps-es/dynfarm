# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/). The section of a version is the text of its GitLab
Release: a tag `v<version>` cannot be released without it (`tools/check_tag.py`).

## [0.1.0] - unreleased

First release: a command-line client for the Dynapps Agent Farm API.

### Added

- **Farms and authentication**: `dynfarm auth login|status|token|logout|switch` for several farms at once. A token is
  checked against the farm's `/whoami` before it is saved, kept in the operating system's keyring (a plain-text file
  readable only by you, with a warning, when there is none) and looked for in `DYNFARM_TOKEN`, the
  keyring and the file, in that order. Every environment variable of the tool starts with `DYNFARM_`; those of other tools
  (such as the ones `farm.py` uses) are not read, though `auth login` offers the key `farm.py` keeps in the keyring, for its own farm only. `auth status` tells an expired token from an invalid one and never prints it. A
  token is never sent over plain `http` except to a loopback address.
- **Tasks**: `dynfarm task run|list|view|watch|logs|result|cancel`, with `last` for your newest task, `--watch` and
  `--exit-status` for scripts, and result downloads checked against the farm's checksum.
- **Agents**: `dynfarm agent list|view|create|edit|delete`, from flags or a JSON file.
- **Schedules**: `dynfarm schedule list|view|create|runs|fire|pause|resume|delete`.
- **Credentials**: `dynfarm credential list|view|set|delete`. The secret is read only from a hidden prompt, standard
  input, `--value-file` or `--value-env`; no option takes it on the command line, and it is sent in the body of one
  request and nowhere else.
- **The rest of the API**: `dynfarm whoami`, `plugin list`, `callback-key`, and `dynfarm api` for any other call, in
  the manner of `gh api`.
- **Output for people and for scripts**: tables on a terminal, tab-separated rows in a pipe, `--json` everywhere;
  errors are branched on the farm's error code, `Retry-After` is honoured on reads, and the exit codes are stable
  (0, 1, 2, 4, and 130 for Ctrl-C while watching a task).
- **`dynfarm completion bash|zsh|fish`** prints the shell's tab-completion script.
- **Installation**: `install.sh` (macOS, Linux) and `install.ps1` (Windows) install `uv` when it is missing, then
  `dynfarm` from the project's package registry (with `DYNFARM_INDEX_USERNAME` and `DYNFARM_INDEX_PASSWORD`, which
  the scripts hand to `uv` for that one install only) or from git.

### Quality gates

- A continuous-integration pipeline that lints, runs the suite on Python 3.11 to 3.14 with a coverage floor, drives the
  installed binary against a fake farm over a real socket, installs the built wheel in a clean environment, and checks
  daily that the commands still cover the live API.
- A tag `v<version>` publishes the wheel and the source distribution to the project's package registry and creates a
  GitLab Release.
