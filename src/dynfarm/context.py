"""What the root command hands to the subcommands through click's `ctx.obj`.

It is a module of its own, free of imports, because `cli.py` builds it on every run and must stay cheap (`dynfarm
--help` does not pay for `httpx`). The three optional fields are seams for tests and for the `auth` commands:

- `transport` replaces the network (`httpx.MockTransport` over the fake farm in the unit tests);
- `token_provider(host) -> str | None` is how a command finds the token of a farm; when it is None,
  `dynfarm.tokens.token_for` answers (environment, keyring, config file);
- `sleep` replaces `time.sleep` so the retry tests do not wait.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class AppContext:
    debug: bool = False
    transport: Any = None
    token_provider: Callable[[str], str | None] | None = None
    sleep: Callable[[float], None] | None = None
