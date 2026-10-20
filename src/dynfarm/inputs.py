"""Reading what a person gives a command in a file: a text (a prompt, an agent's instructions) or a JSON document.

`task run --body-file` and `agent create --from-file` read their input the same way, so the rules live here once:

- `-` is standard input, and a command lets at most one flag read it (the command checks, this module only reads).
- A text loses the newline an editor or a heredoc adds at its end, nothing else.
- An error names the flag and the file and, for JSON, the position of the problem. It never quotes the content: a
  prompt, an instruction or a document may hold what the person did not mean to show on a screen or in a CI log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dynfarm.errors import ConfigError


def read_text(source: str, flag: str) -> str:
    """The text of the file `source` (standard input for `-`) without its final line break; `ConfigError` if not."""
    try:
        # `sys.stdin`, not `click.get_text_stream`, which click 8.5 deprecates. `utf-8-sig`: Windows editors and
        # PowerShell write UTF-8 files with a byte-order mark, which would otherwise be the first character of the
        # prompt or make a JSON file "invalid".
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"cannot read {flag} {source}: {getattr(exc, 'strerror', None) or 'not UTF-8 text'}"
        ) from None
    return text.rstrip("\r\n")


def read_json_object(source: str, flag: str, what: str) -> dict[str, Any]:
    """The JSON object in the file `source`; `what` says what it should describe ("a task", "an agent")."""
    text = read_text(source, flag)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        # Position only: the message quotes nothing, and the file may hold a prompt.
        raise ConfigError(f"{flag} {source} is not valid JSON ({exc.msg} at line {exc.lineno})") from None
    if not isinstance(document, dict):
        raise ConfigError(f"{flag} {source} must hold a JSON object ({what}), not {type(document).__name__}")
    return document
