"""The command modules (`dynfarm.commands.<noun>`), loaded lazily by `cli.COMMANDS`, and what they share.

What lives here is what more than one command needs to ask a person something:

- `is_interactive()`: whether there is a person at a terminal to ask. A prompt without one would block a script or read
  its stdin as an answer, so a command that needs an answer and has no terminal refuses instead (exit 2).
- `confirm()`: the one way a command asks before it removes something: `--yes` skips the question; no terminal
  and no `--yes` is a refusal; "no" changes nothing.

Prompts go to stderr, so stdout carries only the command's output and stays safe to pipe.
"""

from __future__ import annotations

import sys

import click

from dynfarm.errors import ConfigError, DynfarmError


def is_interactive() -> bool:
    """Whether standard input is a terminal (a person can answer a prompt)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):  # replaced or closed stdin
        return False


def confirm(question: str, *, yes: bool) -> None:
    """Return when the person agreed (or passed `--yes`); raise otherwise. Nothing is changed before this returns."""
    if yes:
        return
    if not is_interactive():
        raise ConfigError("not a terminal, so nobody can confirm: pass --yes")
    if not click.confirm(question, default=False, err=True):
        raise DynfarmError("cancelled; nothing was changed")
