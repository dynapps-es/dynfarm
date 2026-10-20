"""`dynfarm completion bash|zsh|fish`: print the shell's tab-completion script.

The script is click's own. It makes the shell call `dynfarm` again with `_DYNFARM_COMPLETE` set, and click answers with
the candidates, so subcommands and options complete without this tool keeping a list of them. Nothing here reads the
configuration, the keyring or the network: printing a script must work on a machine that has never logged in, and a
person pipes it into a shell start-up file, so stdout carries the script and nothing else.
"""

from __future__ import annotations

import click
from click.shell_completion import get_completion_class

from dynfarm._brand import NAME
from dynfarm.cli import cli

SHELLS = ("bash", "zsh", "fish")

#: Where each shell reads a script like this from; shown in `--help`, never written for the person.
_HELP = """Print the completion script for a shell.

\b
  bash   dynfarm completion bash >> ~/.bashrc
  zsh    dynfarm completion zsh  >> ~/.zshrc        (after `autoload -U compinit && compinit`)
  fish   dynfarm completion fish > ~/.config/fish/completions/dynfarm.fish

Open a new shell afterwards. The script only asks `dynfarm` for candidates; it holds none itself.
"""


@click.command("completion", help=_HELP)
@click.argument("shell", type=click.Choice(SHELLS))
def completion(shell: str) -> None:
    # The variable click listens on for this program: `_<PROG>_COMPLETE`, upper-cased, dashes to underscores
    # (`click.core._main_shell_completion`). Derived from the brand name like every other name the tool answers to.
    complete_var = f"_{NAME}_COMPLETE".replace("-", "_").upper()
    click.echo(get_completion_class(shell)(cli, {}, NAME, complete_var).source())
