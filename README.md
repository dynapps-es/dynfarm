# dynfarm

`dynfarm` is a command-line client for the Dynapps Agent Farm API, in the spirit of `gh` and `glab`: every API
operation as a command, several farms with their own tokens kept in the system keyring, and the operations that must
never go through an LLM — handing over a secret — done safely from the terminal.
