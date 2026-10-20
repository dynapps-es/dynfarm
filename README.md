# dynfarm

`dynfarm` is a command-line client for the Dynapps Agent Farm API, in the spirit of `gh` and `glab`: every API
operation as a command, several farms with their own tokens kept in the system keyring, and the operations that must
never go through an LLM — handing over a secret — done safely from the terminal.

## Install

`dynfarm` is a Python package (3.11 or later) that installs as a tool of its own, with its own environment, through
[`uv`](https://docs.astral.sh/uv/). The project is private to Dynapps for now, so every way below needs you to be signed
in to `gitlab.dynapps.be`: either with **git** (the credentials you already use to clone) or with a **GitLab personal
access token** with the `read_api` scope (Preferences, Access tokens). Keep the token in an environment variable, never
in a URL.

**macOS and Linux.** The script installs `uv` when it is missing, then `dynfarm`, then runs `dynfarm --version`:

```bash
git clone https://gitlab.dynapps.be/infra-es/farm-ai/cli.git && sh cli/install.sh        # signed in with git
# or, without cloning, with a token in GITLAB_TOKEN (it travels in a header, not in the URL):
curl -fsSL --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://gitlab.dynapps.be/api/v4/projects/4378/repository/files/install.sh/raw?ref=main" | sh
```

**Windows (PowerShell).**

```powershell
git clone https://gitlab.dynapps.be/infra-es/farm-ai/cli.git; .\cli\install.ps1
# or, without cloning, with a token in $env:GITLAB_TOKEN:
irm -Headers @{ "PRIVATE-TOKEN" = $env:GITLAB_TOKEN } "https://gitlab.dynapps.be/api/v4/projects/4378/repository/files/install.ps1/raw?ref=main" | iex
```

Both scripts install the latest release; a version goes after the script (`sh cli/install.sh 0.1.0`,
`.\cli\install.ps1 -Version 0.1.0`, or `| sh -s -- 0.1.0` when piped). Which source they use depends on one thing:

- **The package registry of the project**, when `DYNFARM_INDEX_USERNAME` (your GitLab user name) and
  `DYNFARM_INDEX_PASSWORD` (a personal access token with the `read_api` scope) are set. The scripts hand them to `uv`
  for the one install and nowhere else: they never put them in a URL, print them or leave them in your shell. The
  registry is `https://gitlab.dynapps.be/api/v4/projects/4378/packages/pypi/simple`.
- **git**, otherwise, at the release tag (`git+https://gitlab.dynapps.be/infra-es/farm-ai/cli.git@v0.1.0`), with your
  git credentials.

**By hand**, with the tool you prefer:

```bash
# git (the registry is what the scripts above are for: they sign uv in without a credential in a URL):
uv tool install git+https://gitlab.dynapps.be/infra-es/farm-ai/cli.git@v0.1.0
pipx install git+https://gitlab.dynapps.be/infra-es/farm-ai/cli.git@v0.1.0
```

If `dynfarm` is not found afterwards, `uv` installed it in a directory that is not on your `PATH` yet: run
`uv tool update-shell` and open a new terminal. Check it with `dynfarm --version`.

## First steps

```bash
dynfarm auth login --hostname https://farm.example.com     # asks for your token (hidden) and checks it with the farm
dynfarm whoami                                             # who the farm thinks you are, and what you may do
dynfarm plugin list                                        # what the farm can run
dynfarm task run developer-skills:code-reviewer --input "Review MR 42 of group/project." --watch
```

Your farm's address and your token come from whoever runs the farm. `dynfarm --help` lists the commands and
`dynfarm COMMAND --help` explains each one. [Commands](#commands) below has the whole list.

### Several farms

A token belongs to one farm, and `dynfarm` keeps one per farm. The first farm you log in to becomes the default; the
others are one flag away:

```bash
dynfarm auth login --hostname https://other-farm.example.com
dynfarm auth status                                         # every farm you are logged in to, and whether it accepts you
dynfarm task list --hostname https://other-farm.example.com # one command against another farm
DYNFARM_HOST=https://other-farm.example.com dynfarm task list   # the same, for a whole shell session or a script
dynfarm auth switch --hostname https://other-farm.example.com    # make it the default
```

A token is never sent over plain `http`, except to `localhost`, `127.0.0.1` or `::1` (for a farm you run on your own
machine).

## Authentication

`dynfarm` keeps one token per farm, like `gh` does per host. There is no built-in farm: you say which one.

```bash
dynfarm auth login --hostname https://farm.example.com      # asks for the token (hidden)
echo "$TOKEN" | dynfarm auth login --hostname https://farm.example.com --with-token   # from a script
dynfarm auth status            # every farm: whose token, where it is kept, and whether the farm accepts it
dynfarm auth switch --hostname https://other-farm.example.com   # change the default farm
dynfarm auth logout --hostname https://other-farm.example.com
```

- **The token is checked before it is kept**: `auth login` calls the farm's `/whoami` with it, and a token the farm
  refuses is not saved (exit 4). The first farm you log in to becomes the default; `--hostname` or `DYNFARM_HOST`
  picks another one for a single command.
- **Where the token is kept**: in the system keyring (macOS Keychain, Windows Credential Manager, Secret Service on
  Linux), under the service `dynfarm` and the farm's URL. With `--insecure-storage`, or when the machine has no
  keyring, it goes in plain text into `config.toml` in the config directory (`DYNFARM_CONFIG_DIR`, by default the
  OS's per-user config directory), readable only by you, and `dynfarm` warns you when it falls back to it.
- **Where the token is looked for**, first answer wins: `DYNFARM_TOKEN`, the keyring, the config file.
  `DYNFARM_TOKEN` answers for whichever farm a command talks to. Every environment variable of `dynfarm` starts with
  `DYNFARM_`: the variables of other tools, such as the token and URL variables `farm.py` uses, are not read.
- **If you already use `farm.py`**: on a terminal `auth login` offers the key `farm.py` keeps in the keyring (service
  `agent-farm-token`), but only for the one farm `farm.py` talks to, `https://farm-dev.ai.dyncloud.es`, and never for
  another. For any other farm, log in with its own token or set `DYNFARM_TOKEN`.
- **`auth status` never prints the token**, only where it came from; it tells an expired token (get a new one) from
  an invalid one (the farm does not accept it), and exits 1 when any farm has a problem.
- **`auth token` is the one command that prints it**, for scripts:
  `curl -H "Authorization: Bearer $(dynfarm auth token)" ...`. Do not run it where its output is recorded.
- **`auth logout` does not revoke the token**: the farm has no endpoint for it, so it keeps working until it expires.
  If it leaked, ask the farm's administrator to revoke it. `logout` asks first on a terminal; scripts pass `--yes`.

### CI

Set `DYNFARM_HOST` and `DYNFARM_TOKEN` (a masked, protected CI variable) and run commands; no `auth login` needed.
When `CI` is set, `dynfarm` never touches a keyring, not even to read. In a GitLab CI job of another project, install it
from this project's registry with `install.sh`, which takes the credentials from variables and never from a URL.
Define `DYNFARM_INDEX_USERNAME` (a GitLab user name) and `DYNFARM_INDEX_PASSWORD` (its `read_api` token) as masked,
protected CI variables of your project, next to `DYNFARM_TOKEN`:

```yaml
review:
  image: python:3.13-slim
  variables:
    DYNFARM_HOST: https://farm.example.com
  script:
    - pip install -q uv
    - >
      curl -fsSL --header "PRIVATE-TOKEN: $DYNFARM_INDEX_PASSWORD"
      "https://gitlab.dynapps.be/api/v4/projects/4378/repository/files/install.sh/raw?ref=main" | sh
    - export PATH="$(uv tool dir --bin):$PATH"
    - dynfarm task run developer-skills:code-reviewer --input "Review MR $CI_MERGE_REQUEST_IID." --exit-status
```

`--exit-status` makes the job fail unless the task succeeded.

## Who you are, and what the farm offers

```bash
dynfarm whoami                 # subject, tenant, scopes and expiry of your token (--json for the farm's answer)
dynfarm plugin list            # the plugins bundled in the agent image and the agents each brings (--json)
dynfarm callback-key           # the public key that verifies task callbacks, as JSON
dynfarm callback-key --pem     # only the PEM, ready for a file: dynfarm callback-key --pem > farm-callback.pem
```

`whoami` is the first thing to run when a command answers "not found" or "not allowed": a task, schedule or
credential belongs to its owner, and the farm answers 404 for what is someone else's.

## Tasks

A task is an agent working on something for you: a catalogue agent with an input, or a free prompt. Launch it, follow
it to the end, read what it produced.

```bash
dynfarm task run developer-skills:code-reviewer --input "Review MR 42 of group/project." --watch
dynfarm task run --prompt-file prompt.txt --scope docs --correlation-id nightly-docs-2026-10-20
dynfarm task run --body-file task.json --exit-status      # a complete task as JSON; exits 1 unless it succeeds
dynfarm task list --state running --state queued          # newest first; --all follows every page, --json for scripts
dynfarm task view last                                    # `last` is the newest task you launched
dynfarm task watch last --exit-status                     # waits on the farm; Ctrl-C stops watching, not the task
dynfarm task logs last --tail 50
dynfarm task result last --download result.json           # summary and link; --download saves the file
dynfarm task cancel last                                  # asks first; --yes in scripts
```

- **What `run` sends.** An `AGENT` (an `agent_id`; it needs an `--input`) or a `--prompt`, never both, plus `--scope`
  (your own label, a lowercase slug), `--session-ref` (continue a finished task's conversation) and
  `--correlation-id` (an idempotency key: repeating it returns the existing task instead of launching another).
  Anything else a task can carry — `workspace`, `marketplace`, `mcp_servers`, `limits`, `callback`, `image`, `services`
  — goes in `--body-file FILE` (`-` for standard input), a complete task as JSON; the flags override its fields.
  `--prompt-file` and `--input-file` read long or multi-line text from a file or from standard input. Contradictions
  (an agent and a prompt, text given twice) are refused with exit 2 before anything is sent.
- **The farm keeps the prompt and the input**, and they can end up in its logs: `run` says so on stderr. Keep secrets
  out of them; credentials have their own command.
- **A launch is never repeated by itself**, not even after a network error, because it might have gone through. Use
  `--correlation-id` and run it again: the farm returns the first task if it did.
- **`run` prints the id and state** (tab-separated when piped: `dynfarm task run ... | cut -f1` is the id) and, with
  `--watch` or `--exit-status` (which implies it), follows the task. `watch` prints each change of state on stderr and
  the finished task on stdout; it exits 0 for any end, or 1 unless `succeeded` with `--exit-status`. Ctrl-C stops
  watching and exits 130; the task keeps running.
- **`task result`** shows the summary, the usage and a link to the full result. `--download PATH` fetches it from the
  object store without sending the farm's token, checks it against the farm's checksum, and will not replace an
  existing file without `--force`. The link expires after a few minutes; run the command again for a new one.
- **A task, schedule or credential that is somebody else's is a 404**, as everywhere in the farm: if `view` says "not
  found" for an id you were given, it belongs to another person or tenant (`dynfarm whoami` says who you are).

## Agents

An agent is a name you launch tasks with (`dynfarm task run NAME`): the farm's own (`platform`, read-only), your
team's (`team`, shared by the whole tenant, written only by a tenant admin) or yours alone (`personal`). You write
the last two.

```bash
dynfarm agent list                                  # slug, scope, owner, description; --scope, --owner me, --json
dynfarm agent view web-manager                      # instructions, what it may touch (credentials, egress), limits
dynfarm agent create web-manager --scope personal --instructions-file web-manager.md --description "Keeps the site up"
dynfarm agent create reviewer --scope team --plugin-agent developer-skills:code-reviewer --requires-workspace
dynfarm agent create etl --scope personal --from-file etl.json     # credentials, mcp_servers, egress, limits...
dynfarm agent edit web-manager --description "Keeps the site up to date"
echo '{"instructions": null}' | dynfarm agent edit web-manager --from-file -     # null clears a field
dynfarm agent delete web-manager                    # asks first; --yes in scripts
```

- **`create` needs `--scope personal|team`** (or `scope` in the file): a team agent is launched by everybody in the
  tenant, so the tool never picks it for you.
- **Flags cover the common fields; the rest goes in a file.** `--description`, `--instructions` /
  `--instructions-file`, `--plugin-agent` and `--requires-workspace` are flags. `mcp_servers`, `credentials`,
  `env_credentials`, `egress`, `limits`, `image`, `services`, `resources`, `sandbox`, `profile` and `work_size` go in
  `--from-file FILE` (`-` for standard input), an agent as JSON; a flag overrides the same field of the file.
  `--instructions-file` reads long or multi-line text from a file or from standard input.
- **`edit` sends only what you gave**, so nothing else changes. `slug` and `scope` never change; there `--scope` only
  says *which* agent when your personal one and your team's share a slug (the personal one is the default, as when a
  task names the agent). `delete` takes `--scope` for the same reason.
- **Instructions are stored by the farm, and every person of the tenant can read a team agent's**: keep secrets out of
  them. An agent that needs a secret names a credential (`credentials`, `env_credentials`) by name in the file.
- The farm decides who may do what: a platform agent answers `agent_read_only`, a team agent needs a tenant admin
  (`insufficient_scope`), another person's personal agent is a 404, and a delete is refused while a schedule that has
  not finished still launches the agent (`agent_in_use_by_schedule`). The error code is printed with the message.

## Schedules

A schedule is a task the farm launches again and again: "review my open MRs every weekday at 08:00". It is a task, a
recurrence (a cron in a timezone) and what to do when a firing is due while the previous task still runs.

```bash
dynfarm schedule create developer-skills:code-reviewer --input "Review my open MRs." \
    --cron "0 8 * * 1-5" --timezone Europe/Madrid
dynfarm schedule create --prompt-file digest.txt --scope docs --cron "0 7 * * 1" --timezone UTC --max-runs 10
dynfarm schedule create --body-file task.json --recurrence-file recurrence.json --overlap allow
dynfarm schedule list                       # id, state, cron, next fire, task, owner; --owner me, --json
dynfarm schedule view SCHEDULE_ID           # when it fires next, how the last firing went, the task it launches
dynfarm schedule runs SCHEDULE_ID           # the tasks it fired, newest first; --limit N for the newest N
dynfarm schedule fire SCHEDULE_ID           # launch its task now, once
dynfarm schedule pause SCHEDULE_ID          # and `resume`
dynfarm schedule delete SCHEDULE_ID         # asks first; --yes in scripts
```

- **The task is written exactly as for `task run`**: an `AGENT` with `--input`, or a `--prompt`, plus `--scope`,
  `--session-ref`, `--prompt-file`, `--input-file` and `--body-file` (a complete task as JSON, for `workspace`,
  `limits`, `mcp_servers`...). There is no `--correlation-id`: every firing generates its own. The farm keeps the
  prompt and the input, as for a task, and `create` says so.
- **The recurrence is `--cron` with `--timezone`**, or `--recurrence-file FILE` (`-` for standard input) with
  `cron`, `timezone`, `starts_at`, `ends_at` and `max_runs`; a flag overrides the same field of the file. The cron has
  five fields and no seconds, so the finest schedule is once a minute. **There is no default timezone**: `create`
  refuses to send a recurrence without one rather than guess yours, because 08:00 means different instants in different
  zones (08:00 `Europe/Madrid` stays 08:00 across daylight-saving changes). `--starts-at` and `--ends-at` are ISO 8601
  times; reaching `--ends-at` or `--max-runs` moves the schedule to `finished`, which is final.
- **`--overlap skip|allow`** (default `skip`): a firing due while the previous task is still running is dropped and
  recorded in `last fire`, or started in parallel.
- **`fire` runs the task now** and prints it (`trigger: manual`); it does not move the next cron firing or count
  against `--max-runs`. Unlike a cron firing, a refusal is an error with a code: `schedule_not_active` (paused or
  finished) or `schedule_overlap`. Follow the task it printed with `dynfarm task watch ID`.
- **`pause` and `resume`** do not replay what was missed meanwhile (`missed runs` in `view` counts it). A finished
  schedule answers `schedule_finished` to both. `create`, `fire`, `pause`, `resume` and `delete` are never repeated by
  themselves after a network error.
- **A schedule belongs to its owner**, and so does every task it fires, whoever pauses or fires it later (a tenant
  admin can operate their colleagues'). Someone else's schedule is a 404. `delete` stops the schedule; the tasks it
  already fired stay under `task list`.
- Cron and timezone are the farm's to judge: `cron_invalid` and `timezone_invalid` are printed with its message.

## Credentials (secrets)

A credential is a secret the farm keeps for you — a git token, an API key, the URL of an MCP server — so a task or an
agent can use it **by name** without the secret ever appearing in a prompt, a task or a log. The farm's channel is
write-only: once stored, no command and no API call gives the value back, only its metadata and a `fingerprint` (a
prefix of its SHA-256) to tell which one is stored. Handing over a secret is the one thing the farm's MCP does not do,
on purpose: it must not pass through an LLM conversation. This command is how you do it.

```bash
dynfarm credential set git-readonly --type git_token                          # asks for it, hidden, twice
dynfarm credential set git-readonly --type git_token --value-env GITLAB_TOKEN # from a variable you already have
dynfarm credential set web-api --type generic --value-file ./web-api.key      # from a file
pass show gitlab/readonly | dynfarm credential set git-readonly --type git_token   # from standard input
dynfarm credential list                    # name, type, scope, fingerprint, expiry; --scope shared, --json
dynfarm credential view git-readonly       # its metadata: type, fingerprint, description, metadata, dates
dynfarm credential delete git-readonly     # asks first; --yes in scripts
```

- **The secret is never an argument.** There is no `--value` option: a command line stays in your shell history, is
  visible to every process on the machine (`ps`) and is printed in CI job logs. `--value SECRET`, `--value=SECRET` or
  the secret as an extra argument is refused (exit 2) without repeating it. The four ways in are:
  1. **a hidden prompt**, on a terminal when no other source is given: asked twice, nothing is saved if the two differ;
  2. **standard input**, when it is not a terminal (or `--value-file -`): a pipe, a heredoc, a password manager;
  3. **`--value-file FILE`**: the file's content;
  4. **`--value-env VARIABLE`**: the content of an environment variable (in CI, a masked variable). Only the
     variable's name is on the command line; an unset variable is an error that names it, never its content.

  One trailing line break is removed from standard input and from a file (the one `echo` or an editor adds), nothing
  else; the environment variable is taken as it is. An empty value is refused before anything is sent.
- **Where the secret goes**: into the body of the one `POST /credentials`, and nowhere else. `dynfarm` does not print
  it, does not write it to any file, and `--debug` never shows a request body or header. A failed write is not
  repeated by itself (the error says when it is safe to repeat it). Tests check all of this on the installed binary
  over a real socket with a sentinel secret.
- **`--type`** is one of `git_token`, `s3`, `model_gateway_key`, `webhook_hmac`, `generic`, `mcp_url` and `mcp_token`,
  checked before the secret is asked for. An `mcp_url` must be a whole `https` URL (`credential_value_invalid`
  otherwise; the farm never echoes the rejected value). `--description`, `--metadata KEY=VALUE` (repeatable) and
  `--expires-at` are **not secret**: the farm shows them to whoever can list the credential.
- **Rotation**: `set` with a name you already have replaces the secret and says `Rotated`; a new name says `Saved`.
  Both print the fingerprint, so you can check which secret is stored without seeing it. A rotation keeps the
  description unless you give a new one (`--description ""` clears it).
- **Deletion** removes the secret and its metadata. Tasks already running keep the copy they were given; the next
  task or schedule that names the credential fails until it is written again.
- **Personal and shared**: a credential is yours by default (`--scope personal`); another person's is a 404, as
  everything in the farm. `--scope shared` stores one for the whole team, which only a tenant admin may write, rotate
  or delete (`insufficient_scope` otherwise); everybody in the tenant can list it, nobody can read it, and the team's
  agents refer to it as `shared:<name>`.
- Store the narrowest secret that does the job (a read-only, short-lived token): the farm hands it to the agents
  that name it, and an agent can be talked into showing what it holds.

## Raw API calls

`dynfarm api` calls any endpoint of the farm's API and prints the JSON answer, like `gh api`. The path is relative to
`/farm/api/v1` (a leading `/farm/api/v1` is accepted too). It is for scripts and for anything the other commands do
not cover; it always prints JSON.

```bash
dynfarm api whoami
dynfarm api -X GET tasks -f state=running -F limit=5        # fields are the query string on GET
dynfarm api tasks --paginate -F limit=50                    # follow next_cursor, merge every page's items
dynfarm api -X POST schedules/SCHEDULE_ID/pause
dynfarm api tasks -f agent_id=developer-skills:code-reviewer -f prompt='review my change'   # POST: a JSON body
echo '{"prompt": "...", "workspace": {"repo": "..."}}' | dynfarm api tasks --input -
dynfarm api whoami -i                                       # status line and headers first
```

- `-f key=value` is a string field; `-F key=value` is typed (`true`, `false`, `null` and integers are converted,
  `@file` or `@-` reads text from a file or standard input). Fields are the query string with `-X GET` and next to
  an `--input` body; otherwise they are a JSON body, and giving fields without `-X` means `POST` (as in `gh api`).
  Nested bodies go through `--input FILE|-`.
- It never asks for confirmation, so `-X DELETE` works in scripts; `dynfarm ... delete` commands ask first.
- The token always comes from the stored login. A full URL, a `..` segment or `-H Authorization: ...` is refused
  (exit 2): `api` cannot send your token anywhere but the selected farm. `--hostname` picks another farm.
- Exit codes are the usual ones: 1 when the farm answers an error, 4 for authentication, 2 for a bad invocation.
  Do not pass a secret with `-f`: it would stay in your shell history. Credentials have their own command.

## Commands

Every command takes `--help`. Those that read from the farm take `--json` (the farm's answer as JSON, for scripts) and
`--hostname URL` (another farm than the default).

| Command | What it does |
|---|---|
| `dynfarm auth login`, `status`, `token`, `logout`, `switch` | Sign in to farms, see where each token is kept, print one for a script, forget one, change the default farm |
| `dynfarm whoami` | Your subject, tenant, scopes and the expiry of your token |
| `dynfarm plugin list` | The plugins bundled in the agent image and the agents each brings |
| `dynfarm callback-key` | The public key that verifies task callbacks |
| `dynfarm task run`, `list`, `view`, `watch`, `logs`, `result`, `cancel` | Launch an agent task, follow it, read its logs and result, stop it |
| `dynfarm agent list`, `view`, `create`, `edit`, `delete` | See the agents you can launch and manage your own and your team's |
| `dynfarm schedule list`, `view`, `create`, `runs`, `fire`, `pause`, `resume`, `delete` | Have the farm launch a task on a cron |
| `dynfarm credential list`, `view`, `set`, `delete` | Hand a secret to the farm (never on the command line), see its metadata, delete it |
| `dynfarm api` | Any other call to the farm's API, like `gh api` |
| `dynfarm completion bash`, `zsh`, `fish` | The shell's tab-completion script |

### Output

On a terminal, tables with relative dates; in a pipe, tab-separated rows without colour codes, so
`dynfarm task list | cut -f1` just works. `--json` prints the farm's answer as it is. Colour is off when `NO_COLOR` is
set. `--debug` (or `DYNFARM_DEBUG`) prints each request's method, path, status and time to stderr, never a header or a
body.

### Exit codes

A script can branch on them:

| Code | Meaning |
|---|---|
| `0` | It worked |
| `1` | The farm answered an error, a network call failed, or a task did not end as `--exit-status` demands |
| `2` | The command was used wrongly or is misconfigured: a bad option, a missing farm, a plain-`http` farm, a destructive command without a terminal and without `--yes` |
| `4` | You need to sign in: no token, or the farm says it is invalid or expired |
| `130` | Ctrl-C while `task watch` (or `run --watch`) was waiting; the task keeps running |

The farm's error code (`not_found`, `insufficient_scope`, `token_expired`...) is printed with its message. Reads that
fail with a retryable error are repeated up to three times, honouring `Retry-After`; a launch, an edit or a delete is
never repeated by itself.

### Environment variables

| Variable | Meaning |
|---|---|
| `DYNFARM_HOST` | The farm to talk to, instead of the default (same as `--hostname`) |
| `DYNFARM_TOKEN` | The token, for whichever farm the command talks to; beats the keyring and the config file |
| `DYNFARM_CONFIG_DIR` | Where `config.toml` lives (default: your OS's per-user config directory: `~/.config/dynfarm` on Linux, `~/Library/Application Support/dynfarm` on macOS, your application-data folder on Windows) |
| `DYNFARM_DEBUG` | Same as `--debug` |
| `CI` | When set, no keyring is touched: the token must come from `DYNFARM_TOKEN` |
| `NO_COLOR` | No colour in the output |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` | Honoured, as by any HTTP client |
| `DYNFARM_INDEX_USERNAME`, `DYNFARM_INDEX_PASSWORD` | Not read by `dynfarm` but by the install scripts: the credentials for the project's package registry (see Install) |

Every variable of `dynfarm` starts with `DYNFARM_`; the others in the table are conventions that are not ours. The
variables of other tools, such as the token and URL variables `farm.py` uses, are not read.

### Shell completion

```bash
dynfarm completion zsh  >> ~/.zshrc        # bash: >> ~/.bashrc
dynfarm completion fish > ~/.config/fish/completions/dynfarm.fish
```

Open a new shell afterwards. Candidates come from `dynfarm` itself, so they are always those of the installed version.

## Upgrade and uninstall

Run the install script again: it installs the latest release (or the one you name) over the current one. By hand, repeat
the `uv tool install` command of your way in with `--reinstall` (and the new tag, for git). `dynfarm --version` says
which one you have.

To remove it: `dynfarm auth logout --hostname URL` for each farm (that deletes the stored token), then
`uv tool uninstall dynfarm`. The config directory (`DYNFARM_CONFIG_DIR`) holds only your farms' addresses and, if the
machine had no keyring, plain-text tokens: delete it too. Logging out does not revoke the token at the farm.

## Releases

Released versions, and what each changed, are in [CHANGELOG.md](CHANGELOG.md); each one is also a GitLab Release of the
project, with the files and the install scripts attached. Contributors: see [CONTRIBUTING.md](CONTRIBUTING.md).
