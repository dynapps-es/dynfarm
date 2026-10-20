#!/bin/sh
# Install dynfarm on macOS or Linux:   sh install.sh [VERSION]      (VERSION is 0.1.0 or v0.1.0; default: the latest)
#
# It installs `uv` (https://docs.astral.sh/uv/) when it is missing, then installs dynfarm from PyPI as a tool of its
# own, in its own environment (`uv tool install dynfarm`, or `dynfarm==VERSION`), and runs `dynfarm --version`.
# Nothing here needs a credential: the package is public. Piped from the web it is
#
#   curl -LsSf https://raw.githubusercontent.com/dynapps-es/dynfarm/main/install.sh | sh
#   curl -LsSf https://raw.githubusercontent.com/dynapps-es/dynfarm/main/install.sh | sh -s -- 0.1.0

set -eu

NAME="dynfarm"
INSTALLER_URL="https://astral.sh/uv/install.sh"

say() { printf '%s\n' "$*"; }
fail() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat << USAGE
Usage: sh install.sh [VERSION]

Installs $NAME from PyPI with uv (installing uv first when it is missing). VERSION is 0.1.0 or v0.1.0; without it,
the latest.
USAGE
}

version=""
for arg in "$@"; do
    case "$arg" in
        -h | --help)
            usage
            exit 0
            ;;
        -*) fail "unknown option '$arg' (try --help)" ;;
        *)
            [ -z "$version" ] || fail "only one version can be given"
            version="${arg#v}"
            [ -n "$version" ] || fail "'$arg' is not a version like 0.1.0"
            ;;
    esac
done
# The version ends up in a requirement: accept what a release is called and nothing else.
case "$version" in
    "" | [0-9]*) ;;
    *) fail "'$version' is not a version like 0.1.0" ;;
esac
case "$version" in
    *[!0-9A-Za-z.+!_-]*) fail "'$version' is not a version like 0.1.0" ;;
esac

tmp_installer=""
cleanup() {
    [ -z "$tmp_installer" ] || rm -f "$tmp_installer"
}
trap cleanup EXIT INT TERM

# -- uv ------------------------------------------------------------------------------------------------------------

if ! command -v uv > /dev/null 2>&1; then
    say "uv is not installed; installing it from $INSTALLER_URL"
    tmp_installer="$(mktemp)"
    # Downloaded to a file first: in a pipe, a failed download would still run whatever half arrived.
    if command -v curl > /dev/null 2>&1; then
        curl -LsSf "$INSTALLER_URL" -o "$tmp_installer" || fail "could not download the uv installer"
    elif command -v wget > /dev/null 2>&1; then
        wget -qO "$tmp_installer" "$INSTALLER_URL" || fail "could not download the uv installer"
    else
        fail "need curl or wget to install uv; install uv yourself (https://docs.astral.sh/uv/) and run this again"
    fi
    sh "$tmp_installer" || fail "the uv installer failed"
    # Where uv's installer puts it, unless the person moved it with XDG_BIN_HOME.
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
    export PATH
    command -v uv > /dev/null 2>&1 || fail "uv was installed but is not on PATH; open a new shell and run this again"
fi

# -- dynfarm -------------------------------------------------------------------------------------------------------

requirement="$NAME"
[ -z "$version" ] || requirement="$NAME==$version"
say "Installing $requirement from PyPI"
uv tool install --reinstall "$requirement" || fail "uv could not install $requirement from PyPI (is the version published?)"

# -- check ---------------------------------------------------------------------------------------------------------

bin_dir="$(uv tool dir --bin)"
if command -v "$NAME" > /dev/null 2>&1; then
    command_path="$(command -v "$NAME")"
else
    command_path="$bin_dir/$NAME"
    say "$bin_dir is not on your PATH yet: run 'uv tool update-shell' and open a new shell."
fi
"$command_path" --version || fail "$NAME was installed but does not run"

say ""
say "Next: $NAME auth login --hostname https://<your farm>"
