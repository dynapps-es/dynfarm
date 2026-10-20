# dynfarm

`dynfarm` is a command-line client for the Dynapps Agent Farm API, in the spirit of `gh` and `glab`: every API
operation as a command, several farms with their own tokens kept in the system keyring, and the operations that must
never go through an LLM — handing over a secret — done safely from the terminal.

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
- **Where the token is looked for**, first answer wins: `DYNFARM_TOKEN`, `AGENT_FARM_TOKEN` (the variable `farm.py`
  uses), the keyring, the config file. `DYNFARM_TOKEN` answers for whichever farm a command talks to;
  `AGENT_FARM_TOKEN` only for `farm.py`'s own farm (below).
- **If you already use `farm.py`**: `AGENT_FARM_TOKEN` and `AGENT_FARM_URL` work as they are, and on a terminal
  `auth login` offers the key `farm.py` keeps in the keyring (service `agent-farm-token`). Both belong to the one farm
  `farm.py` talks to — `AGENT_FARM_URL`, or `https://farm-dev.ai.dyncloud.es` when it is unset — so `dynfarm` uses
  them only for that farm and never sends them to another one (`auth status` says when `AGENT_FARM_TOKEN` is ignored
  for a farm). For any other farm, log in or set `DYNFARM_TOKEN`.
- **`auth status` never prints the token**, only where it came from; it tells an expired token (get a new one) from
  an invalid one (the farm does not accept it), and exits 1 when any farm has a problem.
- **`auth token` is the one command that prints it**, for scripts:
  `curl -H "Authorization: Bearer $(dynfarm auth token)" ...`. Do not run it where its output is recorded.
- **`auth logout` does not revoke the token**: the farm has no endpoint for it, so it keeps working until it expires.
  If it leaked, ask the farm's administrator to revoke it. `logout` asks first on a terminal; scripts pass `--yes`.

### CI

Set `DYNFARM_HOST` and `DYNFARM_TOKEN` (a masked, protected CI variable) and run commands; no `auth login` needed.
When `CI` is set, `dynfarm` never touches a keyring, not even to read.

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
