"""The tool's name, in one place.

Everything a person sees or configures that carries the name derives from `NAME`: the command (it must match the
`[project.scripts]` key in `pyproject.toml`, which a test enforces), the environment variables (`DYNFARM_HOST`,
`DYNFARM_TOKEN`, ...), the config directory, the keyring service and the `User-Agent`. A rename or a white-label build
is this constant plus that one key in `pyproject.toml`.

The `LEGACY_*` names are those of `farm.py`, the client of the `farm-skills` Claude Code plugin. They are read with a
lower precedence than ours so that someone who already uses `farm.py` or the farm's MCP configures nothing new.
"""

from __future__ import annotations

import platform

from dynfarm import __version__

NAME = "dynfarm"

#: Prefix of every environment variable the tool reads: `DYNFARM_` for `dynfarm`.
ENV_PREFIX = NAME.upper().replace("-", "_") + "_"

#: Service name under which tokens are kept in the OS keyring; the account is the farm's URL.
KEYRING_SERVICE = NAME

LEGACY_ENV_TOKEN = "AGENT_FARM_TOKEN"
LEGACY_ENV_HOST = "AGENT_FARM_URL"
#: Keychain service where `farm.py` tells people to keep their key (one key, no host).
LEGACY_KEYRING_SERVICE = "agent-farm-token"


def env(suffix: str) -> str:
    """Name of one of the tool's environment variables: `env("TOKEN")` is `DYNFARM_TOKEN`."""
    return ENV_PREFIX + suffix


def user_agent() -> str:
    """`dynfarm/<version> (python <x.y.z>; <os>)`: lets the farm's logs tell CLI versions apart."""
    return f"{NAME}/{__version__} (python {platform.python_version()}; {platform.system().lower() or 'unknown'})"
