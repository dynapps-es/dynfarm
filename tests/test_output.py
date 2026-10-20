"""One printing path for every command: JSON for scripts, aligned tables for people, tab-separated rows for pipes.

Scripts depend on `--json` being the API's own answer and on piped output being easy to cut; people depend on a table
that lines up and on dates they can read. Both depend on text from other people (a task prompt) never being able to
drive the terminal, which is why control characters are cleaned from table cells.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from dynfarm.output import Column, emit, relative_time, use_color

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

ANSWER = {
    "items": [
        {
            "task_id": "t1",
            "state": "running",
            "agent_id": "repo-helper",
            "created_at": "2026-10-20T09:00:00Z",
            "usage": {"cost_eur": 0.5},
            "tags": ["a", "b"],
            "ok": True,
        },
        {
            "task_id": "t2",
            "state": "succeeded",
            "agent_id": None,
            "created_at": "2026-10-19T12:00:00Z",
            "usage": None,
            "tags": [],
            "ok": False,
        },
    ],
    "next_cursor": None,
}
COLUMNS = (
    Column("id", "task_id"),
    Column("state", "state"),
    Column("agent", "agent_id"),
    Column("created", "created_at", "date"),
)


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def render(data=ANSWER, *, tty: bool, **kwargs) -> str:
    stream = Terminal() if tty else io.StringIO()
    emit(data, stream=stream, now=NOW, **kwargs)
    return stream.getvalue()


# -- JSON -------------------------------------------------------------------------------------------------------


def test_json_is_the_answer_verbatim_indented_and_in_the_farms_key_order():
    out = render(tty=True, json_flag=True, table=COLUMNS)
    assert json.loads(out) == ANSWER
    assert out.startswith('{\n  "items": [\n    {\n      "task_id": "t1"')
    assert list(json.loads(out)["items"][0]) == list(ANSWER["items"][0])


def test_json_does_not_escape_non_ascii_and_still_escapes_control_characters():
    out = render({"summary": "café\x1b[31m"}, tty=False, json_flag=True)
    assert "café" in out
    assert "\\u001b" in out
    assert "\x1b" not in out


def test_a_command_with_no_table_prints_json_whatever_the_terminal():
    assert json.loads(render({"keys": []}, tty=True)) == {"keys": []}


# -- tables on a terminal ---------------------------------------------------------------------------------------


def test_a_terminal_gets_a_table_with_headers_aligned_and_dates_relative(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    lines = render(tty=True, table=COLUMNS).splitlines()
    assert lines[0] == "ID  STATE      AGENT        CREATED"
    assert lines[1] == "t1  running    repo-helper  3 hours ago"
    assert lines[2] == "t2  succeeded  -            1 day ago"


def test_no_line_of_a_table_ends_in_spaces(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert all(line == line.rstrip() for line in render(tty=True, table=COLUMNS).splitlines())


def test_headers_are_bold_on_a_terminal_unless_no_color_is_set(monkeypatch):
    assert "\x1b[1mID\x1b[0m" in render(tty=True, table=COLUMNS)
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in render(tty=True, table=COLUMNS)
    monkeypatch.setenv("NO_COLOR", "")
    assert "\x1b" in render(tty=True, table=COLUMNS)


def test_colour_is_never_used_off_a_terminal_or_on_a_dumb_one(monkeypatch):
    assert use_color(Terminal())
    assert not use_color(io.StringIO())
    monkeypatch.setenv("TERM", "dumb")
    assert not use_color(Terminal())


def test_wide_characters_are_counted_by_the_columns_they_take(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    rows = [{"a": "日本", "b": "x"}, {"a": "ab", "b": "y"}]
    lines = render(rows, tty=True, table=(Column("a", "a"), Column("b", "b"))).splitlines()
    assert lines[1] == "日本  x"
    assert lines[2] == "ab    y"


def test_an_empty_listing_on_a_terminal_says_so_on_stderr_and_prints_nothing_else(capsys):
    assert render({"items": []}, tty=True, table=COLUMNS) == ""
    assert capsys.readouterr().err == "no results\n"


def test_a_single_object_is_a_one_row_table():
    out = render({"task_id": "t9", "state": "queued", "agent_id": "a", "created_at": None}, tty=True, table=COLUMNS)
    assert out.splitlines()[1] == "t9  queued  a      -"


def test_a_list_given_directly_is_the_rows():
    assert render(ANSWER["items"], tty=False, table=COLUMNS).count("\n") == 2


# -- rows in a pipe ---------------------------------------------------------------------------------------------


def test_a_pipe_gets_tab_separated_rows_without_headers_and_iso_dates():
    assert render(tty=False, table=COLUMNS) == (
        "t1\trunning\trepo-helper\t2026-10-20T09:00:00Z\nt2\tsucceeded\t\t2026-10-19T12:00:00Z\n"
    )


def test_a_pipe_has_no_output_at_all_for_an_empty_listing():
    assert render({"items": []}, tty=False, table=COLUMNS) == ""


def test_values_are_formatted_by_type_and_dotted_keys_and_functions_find_nested_values():
    columns = (
        Column("cost", "usage.cost_eur"),
        Column("tags", "tags"),
        Column("ok", "ok"),
        Column("n", lambda row: len(row["task_id"])),
        Column("missing", "nope.deeper"),
    )
    assert render(tty=False, table=columns).splitlines() == ["0.5\ta, b\ttrue\t2\t", "\t\tfalse\t2\t"]


def test_dict_values_are_compact_json():
    assert render([{"m": {"a": 1}}], tty=False, table=(Column("m", "m"),)) == '{"a":1}\n'


@pytest.mark.parametrize("tty", [True, False])
def test_control_characters_from_the_api_cannot_drive_the_terminal_or_break_a_row(tty):
    nasty = {"v": "a\x1b[2Jb\tc\nd\re\x9bf\x7f"}
    out = render([nasty], tty=tty, table=(Column("v", "v"),))
    body = out.splitlines()[-1]
    assert "\x1b" not in body and "\x9b" not in body
    assert out.count("\n") == (2 if tty else 1)
    assert "\t" not in body


# -- dates ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "less than a minute ago"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=59), "59 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=23), "23 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=29), "29 days ago"),
        (timedelta(days=45), "2026-09-05"),
        (timedelta(seconds=-5), "in less than a minute"),
        (timedelta(hours=-2), "in 2 hours"),
        (timedelta(days=-3), "in 3 days"),
    ],
)
def test_relative_dates_read_naturally_and_fall_back_to_the_date(delta, expected):
    assert relative_time(NOW - delta, NOW) == expected


def test_a_naive_date_is_taken_as_utc_and_an_unparseable_one_is_shown_as_sent():
    assert relative_time(datetime(2026, 10, 20, 11, 0, 0), NOW) == "1 hour ago"
    out = render([{"d": "not a date"}, {"d": "2026-10-20T11:00:00+02:00"}], tty=True, table=(Column("d", "d", "date"),))
    assert out.splitlines()[1:] == ["not a date", "3 hours ago"]
