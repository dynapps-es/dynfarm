"""`dynfarm person`: find the people the farm knows, and offboard or readmit one.

    person list              GET  /people               person view ID          GET  /people/{person_id}
    person offboard ID       POST /people/{id}/offboard  person readmit ID       POST /people/{id}/readmit

Every person and every integration's key the farm has seen in a tenant has a `person_id` (a UUID) that names them
there. The commands are for a tenant admin; a key with `farm:platform:read` can also read other tenants (`list
--tenant`), never change them.

`offboard` is a dry run unless `--apply` is given. The dry run lists by name and count what would be removed (running
tasks, schedules, personal agents and credentials, chat links) and what the person created for the team that passes
to someone else (`--transfer-to`, or you), and changes nothing. With `--apply` the same report is shown first and the
person's `subject` has to be typed to go on (`--yes` for a script); the offboarding then cannot be undone, only the
block can be lifted with `readmit`. If the cluster or the secret store does not answer half way, the farm says what
was already done and the same command finishes it.
"""

from __future__ import annotations

from typing import Any

import click

from dynfarm import _brand, commands
from dynfarm.errors import ConfigError, DynfarmError, FarmError
from dynfarm.http import FarmClient, needs_client, segment
from dynfarm.output import Column, clean_text, emit
from dynfarm.registry import operation

JSON_HELP = "Print the farm's answer as JSON."

PERSON_COLUMNS = (
    Column("person_id", "person_id"),
    Column("subject", "subject"),
    Column("kind", "kind"),
    Column("tenant", "tenant"),
    Column("admin seen", "admin_seen_at", "date"),
    Column("offboarded", "offboarded_at", "date"),
)


@click.group()
def person() -> None:
    """Find the people of a tenant, offboard one in a single step, or let them back in."""


# -- list ---------------------------------------------------------------------------------------------------------


@person.command("list")
@click.option("--kind", type=click.Choice(["person", "service"]), help="Only people, or only integrations' keys.")
@click.option(
    "--offboarded/--active",
    default=None,
    help="Only the ones who were offboarded, or only the others. Without it, everybody.",
)
@click.option(
    "--tenant",
    metavar="SLUG|*",
    help="Another tenant, or `*` for all (only a key with farm:platform:read; read-only).",
)
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("list_people")
@needs_client
def list_(client: FarmClient, kind: str | None, offboarded: bool | None, tenant: str | None, json_flag: bool) -> None:
    """List the people and service keys the farm knows in your tenant (tenant admins), with their person_id."""
    params = {"kind": kind, "offboarded": offboarded, "tenant": tenant}
    try:
        found = client.request("GET", "/people", params=params, operation_id="list_people")
    except FarmError as exc:
        raise _explain_tenant(exc, tenant) from None
    emit(found, json_flag=json_flag, table=PERSON_COLUMNS)


# -- view ---------------------------------------------------------------------------------------------------------


@person.command()
@click.argument("person_id")
@click.option("--tenant", metavar="SLUG", help="Another tenant (only a key with farm:platform:read).")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("get_person")
@needs_client
def view(client: FarmClient, person_id: str, tenant: str | None, json_flag: bool) -> None:
    """Show one person by person_id."""
    try:
        found = client.request(
            "GET", f"/people/{segment(person_id)}", params={"tenant": tenant}, operation_id="get_person"
        )
    except FarmError as exc:
        raise _explain_tenant(exc, tenant) from None
    if json_flag:
        emit(found, json_flag=True)
        return
    rows = [
        ("person_id", found.get("person_id")),
        ("subject", found.get("subject")),
        ("kind", found.get("kind")),
        ("tenant", found.get("tenant")),
        ("first seen", found.get("first_seen_at")),
        ("admin seen", found.get("admin_seen_at")),
        ("offboarded", found.get("offboarded_at")),
        ("offboarded by", found.get("offboarded_by")),
    ]
    _print_rows(rows)


# -- offboard -----------------------------------------------------------------------------------------------------


@person.command()
@click.argument("person_id")
@click.option(
    "--transfer-to",
    metavar="PERSON_ID",
    help="Who receives the team agents and shared credentials the person created (a person of the tenant). "
    "Default: you.",
)
@click.option("--apply", "apply_", is_flag=True, help="Do it. Without this, only a dry run: nothing is changed.")
@click.option("--yes", "-y", is_flag=True, help="With --apply, do not ask to type the subject.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("offboard_person")
@needs_client
def offboard(
    client: FarmClient, person_id: str, transfer_to: str | None, apply_: bool, yes: bool, json_flag: bool
) -> None:
    """Offboard a person: cancel their tasks, remove their schedules, agents and credentials, and block them.

    A dry run by default. --apply asks you to type the person's subject first. It cannot be undone (readmit only lifts
    the block).
    """
    path = f"/people/{segment(person_id)}/offboard"
    body: dict[str, Any] = {"dry_run": True}
    if transfer_to:
        body["transfer_to"] = transfer_to
    report = client.request("POST", path, json=body, operation_id="offboard_person")
    subject = report["person"]["subject"]
    if not apply_:
        if json_flag:
            emit(report, json_flag=True)
            return
        _print_report(report)
        click.echo(f"Nothing was changed. Run again with --apply to offboard {clean_text(subject)}.")
        return
    if not json_flag:
        _print_report(report)
    _confirm_subject(subject, yes=yes)
    try:
        done = client.request("POST", path, json={**body, "dry_run": False}, operation_id="offboard_person")
    except FarmError as exc:
        raise _interrupted(exc) from None
    if json_flag:
        emit(done, json_flag=True)
        return
    _print_report(done)


def _confirm_subject(subject: str, *, yes: bool) -> None:
    """The person types the subject: the one thing that cannot be a habit, because the offboarding cannot be undone."""
    if yes:
        return
    if not commands.is_interactive():
        raise ConfigError("not a terminal, so nobody can confirm: pass --yes")
    typed = click.prompt(f"Type {subject} to offboard them", default="", show_default=False, err=True)
    if typed.strip() != subject:
        raise DynfarmError("cancelled; nothing was changed")


def _interrupted(exc: FarmError) -> FarmError:
    """A `503` that says what was already done: show it, and say that repeating the command finishes the job."""
    done = exc.details.get("done") if isinstance(exc.details, dict) else None
    if exc.status != 503 or not done:
        return exc
    click.echo(f"Already done (it stays done): {_inline(done)}", err=True)
    message = f"{exc.message}. The offboarding stopped half way; run the same command again to finish it"
    return FarmError(
        exc.status,
        exc.code,
        message,
        retryable=exc.retryable,
        details=exc.details,
        retry_after=exc.retry_after,
        correlation_id=exc.correlation_id,
    )


def _inline(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={_inline(item)}" for key, item in value.items())
    return str(value)


def _print_report(report: dict[str, Any]) -> None:
    """The offboarding report for a person: names and counts only (the farm never sends a value)."""
    who = report["person"]
    state = "Would offboard" if report["dry_run"] else "Offboarded"
    click.echo(f"{state} {clean_text(who['subject'])} ({who['kind']}, {who['tenant']}, person_id {who['person_id']})")
    counts = report["counts"]
    sections = [
        ("tasks", [f"{item['task_id']} ({item['state']}, {item['agent_id']})" for item in report["tasks"]]),
        (
            "schedules",
            [
                f"{item['schedule_id']} ({item.get('name') or 'unnamed'}, {item['state']})"
                for item in report["schedules"]
            ],
        ),
        ("agents", list(report["agents"])),
        ("credentials", [f"{item['name']} ({item['type']})" for item in report["credentials"]]),
        ("chat links", [f"{item['channel']}: {item['count']}" for item in report["channel_links"]]),
    ]
    counted = {"chat links": counts["channel_links"]}
    for label, items in sections:
        total = counted.get(label, counts.get(label, len(items)))
        click.echo(f"  {label}: {total}")
        for item in items:
            click.echo(f"    - {clean_text(str(item))}")
    for label, key in (("link codes", "link_codes"), ("upload tickets", "upload_tickets")):
        click.echo(f"  {label}: {counts[key]}")
    transfer = report["transfer"]
    recipient = transfer.get("to")
    if recipient:
        click.echo(f"  passes to {clean_text(recipient['subject'])} ({recipient['kind']}):")
        click.echo(f"    team agents: {', '.join(transfer['agents']) or 'none'}")
        click.echo(f"    shared credentials: {', '.join(transfer['credentials']) or 'none'}")
    for person_in_channel in report["channel_people"]:
        click.echo(f"  channel list: {person_in_channel['channel']} (active: {person_in_channel['active']})")
    for warning in (report["service_warning"], transfer.get("warning"), report["channel_people_warning"]):
        if warning:
            click.echo(f"warning: {clean_text(warning)}")
    click.echo(f"  history: {clean_text(report['history']['note'])}")


# -- readmit ------------------------------------------------------------------------------------------------------


@person.command()
@click.argument("person_id")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@click.option("--json", "json_flag", is_flag=True, help=JSON_HELP)
@operation("readmit_person")
@needs_client
def readmit(client: FarmClient, person_id: str, yes: bool, json_flag: bool) -> None:
    """Lift the block on an offboarded person. What the offboarding removed does not come back."""
    commands.confirm(f"Readmit person {person_id}? What the offboarding removed does not come back.", yes=yes)
    found = client.request("POST", f"/people/{segment(person_id)}/readmit", operation_id="readmit_person")
    if json_flag:
        emit(found, json_flag=True)
        return
    click.echo(f"readmitted {clean_text(found['subject'])}; they can use the farm again")
    click.echo(
        f"What was removed is not restored. To check: `{_brand.NAME} person view {found['person_id']}`.", err=True
    )


# -- shared -------------------------------------------------------------------------------------------------------


def _explain_tenant(exc: FarmError, tenant: str | None) -> FarmError:
    """`--tenant` needs a platform key: say so when the farm refuses it with a 403."""
    if tenant is None or exc.status != 403:
        return exc
    return FarmError(
        exc.status,
        exc.code,
        f"{exc.message} (--tenant only works with a key that has farm:platform:read)",
        retryable=exc.retryable,
        details=exc.details,
        retry_after=exc.retry_after,
        correlation_id=exc.correlation_id,
    )


def _print_rows(rows: list[tuple[str, Any]]) -> None:
    shown = [(label, value) for label, value in rows if value not in (None, "")]
    width = max(len(label) for label, _ in shown) + 1
    for label, value in shown:
        click.echo(f"{label + ':':<{width}} {clean_text(str(value))}")
