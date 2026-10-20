"""One journey per resource, as a person or a CI job lives it, on the installed binary over a real socket.

Why these tests exist: each command already has unit tests, but a regression that matters is one that breaks the way
the commands work *together* through the real entry point: a login whose token the next command cannot find, a task
watched to its end whose result cannot be downloaded, a schedule created and then not deletable. Each test walks one
resource from its first command to its last and checks the exit code of every step, what reached the farm, and what
stayed off it (the token on the object store, the secret everywhere but the body of one request).
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys

import pytest
from fakefarm import ALICE

CREDENTIALS = "/farm/api/v1/credentials"


def asked(farm, method: str, operation_id: str) -> list:
    return [r for r in farm.requests if r.method == method and r.operation_id == operation_id]


# -- identity ---------------------------------------------------------------------------------------------------


def test_a_person_logs_in_asks_who_they_are_and_logs_out(dynfarm, fake_farm):
    login = dynfarm.login()
    assert login.code == 0, login.output
    assert ALICE not in login.output
    assert "alice" in login.stderr

    config_file = dynfarm.config_dir / "config.toml"
    assert config_file.is_file()
    if sys.platform != "win32":
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600

    whoami = dynfarm.run("whoami", "--json")
    assert whoami.code == 0, whoami.output
    assert whoami.json()["subject"] == "alice" and whoami.json()["tenant"] == "tenant-a"
    status = dynfarm.run("auth", "status")
    assert status.code == 0 and "logged in as alice" in status.stdout and ALICE not in status.output

    logout = dynfarm.run("auth", "logout", "--yes")
    assert logout.code == 0, logout.output
    after = dynfarm.run("whoami")
    assert after.code == 4 and "dynfarm auth login" in after.stderr
    # Every call that carried the token carried it in the header and nowhere else.
    assert all(ALICE not in request.target for request in fake_farm.requests)


def test_a_login_with_a_token_the_farm_rejects_saves_nothing(dynfarm):
    run = dynfarm.run(
        "auth", "login", "--hostname", dynfarm.url, "--with-token", "--insecure-storage", input="tok-not-a-token\n"
    )
    assert run.code == 4, run.output
    assert not (dynfarm.config_dir / "config.toml").exists()
    assert dynfarm.run("whoami").code == 4


def test_the_token_of_the_environment_works_without_a_login_and_is_never_written(dynfarm):
    run = dynfarm.run("whoami", "--json", env={"DYNFARM_TOKEN": ALICE})
    assert run.code == 0, run.output
    assert run.json()["subject"] == "alice"
    assert not dynfarm.config_dir.exists() or ALICE not in "".join(
        p.read_text(encoding="utf-8") for p in dynfarm.config_dir.rglob("*") if p.is_file()
    )


def test_the_catalogue_commands_answer_with_what_the_farm_has(logged_in):
    plugins = logged_in.run("plugin", "list", "--json")
    assert plugins.code == 0 and {p["name"] for p in plugins.json()["items"]} >= {"example-plugin"}
    key = logged_in.run("callback-key")
    assert key.code == 0 and key.json()["keys"]


# -- agents -----------------------------------------------------------------------------------------------------


def test_an_agent_is_created_listed_viewed_and_deleted(logged_in):
    created = logged_in.run("agent", "create", "web-manager", "--scope", "personal", "--instructions", "Be brief.")
    assert created.code == 0, created.output

    listing = logged_in.run("agent", "list", "--json")
    assert listing.code == 0 and "web-manager" in [a["slug"] for a in listing.json()["items"]]
    mine = logged_in.run("agent", "list", "--owner", "me", "--json")
    assert [a["slug"] for a in mine.json()["items"]] == ["web-manager"]

    viewed = logged_in.run("agent", "view", "web-manager", "--json")
    assert viewed.code == 0 and viewed.json()["scope"] == "personal"

    edited = logged_in.run("agent", "edit", "web-manager", "--description", "Keeps the site tidy.")
    assert edited.code == 0, edited.output
    assert logged_in.run("agent", "view", "web-manager", "--json").json()["description"] == "Keeps the site tidy."

    deleted = logged_in.run("agent", "delete", "web-manager", "--yes")
    assert deleted.code == 0, deleted.output
    gone = logged_in.run("agent", "view", "web-manager")
    assert gone.code == 1 and "agent_not_found" in gone.stderr


# -- tasks ------------------------------------------------------------------------------------------------------


def test_a_task_is_launched_watched_logged_and_its_result_downloaded_without_the_farms_token(
    logged_in, fake_farm, tmp_path, finish_after_creation
):
    finish_after_creation({"state": "running"}, {"state": "succeeded"})
    run = logged_in.run("task", "run", "--prompt", "Summarise the open TODOs.", "--exit-status", "--json")
    assert run.code == 0, run.output
    assert [line for line in run.stderr.splitlines() if line in ("running", "succeeded")] == ["running", "succeeded"]
    finished = run.json()
    assert finished["state"] == "succeeded"
    task_id = finished["task_id"]
    assert [r.query.get("wait") for r in asked(fake_farm, "GET", "get_task")][0] == ["30"]

    fake_farm.set_task(task_id, state="succeeded")
    logs = logged_in.run("task", "logs", task_id, "--json")
    assert logs.code == 0 and logs.json()["task_id"] == task_id

    target = tmp_path / "result.json"
    result = logged_in.run("task", "result", "last", "--download", str(target), "--json")
    assert result.code == 0, result.output
    link = result.json()["result"]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == link["sha256"]
    assert json.loads(target.read_bytes())["task_id"] == task_id
    store = [r for r in fake_farm.requests if r.path.startswith("/_s3/")]
    assert [r.method for r in store] == ["GET"] and "authorization" not in store[0].headers

    again = logged_in.run("task", "result", "last", "--download", str(target))
    assert again.code == 2 and "--force" in again.stderr
    assert logged_in.run("task", "result", "last", "--download", str(target), "--force").code == 0


def test_a_task_that_fails_makes_exit_status_exit_1(logged_in, finish_after_creation):
    finish_after_creation({"state": "failed", "failure_reason": "timeout"})
    run = logged_in.run("task", "run", "--prompt", "Do it.", "--exit-status")
    assert run.code == 1, run.output
    assert "failed" in run.stdout


def test_a_running_task_is_cancelled_only_with_a_confirmation(logged_in, fake_farm):
    task_id = fake_farm.seed_task(ALICE, state="running")
    refused = logged_in.run("task", "cancel", task_id)
    assert refused.code == 2 and "--yes" in refused.stderr
    assert asked(fake_farm, "DELETE", "cancel_task") == []

    cancelled = logged_in.run("task", "cancel", "last", "--yes", "--json")
    assert cancelled.code == 0 and cancelled.json()["state"] == "cancelled"
    assert logged_in.run("task", "list", "--state", "cancelled", "--json").json()["items"][0]["task_id"] == task_id


def test_a_task_list_is_paged_to_the_end_with_the_cursor(logged_in, fake_farm):
    seeded = [fake_farm.seed_task(ALICE, state="succeeded") for _ in range(3)]
    first = logged_in.run("task", "list", "--limit", "2", "--json")
    assert first.code == 0, first.output
    assert len(first.json()["items"]) == 2 and first.json()["next_cursor"]
    everything = logged_in.run("task", "list", "--json", "--limit", "100")
    assert {t["task_id"] for t in everything.json()["items"]} == set(seeded)


# -- schedules --------------------------------------------------------------------------------------------------


def test_a_schedule_is_created_paused_resumed_fired_and_deleted(logged_in):
    created = logged_in.run(
        "schedule",
        "create",
        "--prompt",
        "Digest of the week.",
        "--cron",
        "0 8 * * 1",
        "--timezone",
        "Europe/Madrid",
        "--json",
    )
    assert created.code == 0, created.output
    schedule_id = created.json()["schedule_id"]
    assert created.json()["recurrence"]["cron"] == "0 8 * * 1"

    assert schedule_id in logged_in.run("schedule", "list").stdout
    paused = logged_in.run("schedule", "pause", schedule_id, "--json")
    assert paused.code == 0 and paused.json()["state"] == "paused"
    cannot_fire = logged_in.run("schedule", "fire", schedule_id)
    assert cannot_fire.code == 1 and "schedule_not_active" in cannot_fire.stderr
    resumed = logged_in.run("schedule", "resume", schedule_id, "--json")
    assert resumed.code == 0 and resumed.json()["state"] == "active"

    fired = logged_in.run("schedule", "fire", schedule_id, "--json")
    assert fired.code == 0, fired.output
    runs = logged_in.run("schedule", "runs", schedule_id, "--json")
    assert runs.code == 0 and len(runs.json()["items"]) == 1

    assert logged_in.run("schedule", "delete", schedule_id).code == 2  # no terminal, no --yes
    assert logged_in.run("schedule", "delete", schedule_id, "--yes").code == 0
    missing = logged_in.run("schedule", "view", schedule_id)
    assert missing.code == 1 and "not_found" in missing.stderr


# -- credentials ------------------------------------------------------------------------------------------------


def test_a_credential_is_set_from_stdin_listed_without_its_value_and_deleted(logged_in, fake_farm):
    secret = "sk-e2e-sentinel-0f3a9c1d7b5e"
    saved = logged_in.run("credential", "set", "git-readonly", "--type", "git_token", input=secret + "\n")
    assert saved.code == 0, saved.output
    assert secret not in saved.output
    posts = [r for r in fake_farm.requests if r.method == "POST" and r.path == CREDENTIALS]
    assert [json.loads(r.body)["value"] for r in posts] == [secret]
    assert [r.path for r in fake_farm.requests if secret.encode() in r.body] == [CREDENTIALS]

    listed = logged_in.run("credential", "list", "--json")
    assert listed.code == 0 and secret not in listed.output
    assert [c["name"] for c in listed.json()["items"]] == ["git-readonly"]
    viewed = logged_in.run("credential", "view", "git-readonly", "--json")
    assert viewed.code == 0 and viewed.json()["type"] == "git_token" and secret not in viewed.output

    assert logged_in.run("credential", "delete", "git-readonly").code == 2
    assert logged_in.run("credential", "delete", "git-readonly", "--yes").code == 0
    assert logged_in.run("credential", "list", "--json").json()["items"] == []


@pytest.mark.parametrize("how", ["file", "env"])
def test_a_credential_set_from_a_file_or_the_environment_keeps_the_secret_off_the_command_line(
    logged_in, fake_farm, tmp_path, how
):
    secret = f"sk-e2e-{how}-3c91e5a7d2b8"
    if how == "file":
        path = tmp_path / "secret.txt"
        path.write_bytes(secret.encode() + b"\n")
        run = logged_in.run("credential", "set", "web-api", "--type", "generic", "--value-file", str(path))
    else:
        run = logged_in.run(
            "credential", "set", "web-api", "--type", "generic", "--value-env", "E2E_SECRET", env={"E2E_SECRET": secret}
        )
    assert run.code == 0, run.output
    assert secret not in " ".join(run.argv) and secret not in run.output
    assert [
        json.loads(r.body)["value"] for r in fake_farm.requests if r.path == CREDENTIALS and r.method == "POST"
    ] == [secret]
