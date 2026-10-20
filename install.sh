#!/bin/sh
# Install dynfarm on macOS or Linux:   sh install.sh [VERSION]      (VERSION is 0.1.0 or v0.1.0; default: the latest)
#
# It installs `uv` (https://docs.astral.sh/uv/) when it is missing, then installs dynfarm as a tool of its own, in its
# own environment, from one of two places:
#
#   the project's package registry   when UV_INDEX_DYNFARM_USERNAME and UV_INDEX_DYNFARM_PASSWORD are set (a GitLab
#                                    user name and a personal access token with the `read_api` scope).
#   git                              otherwise, with the git credentials you already have for gitlab.dynapps.be.
#
# A token never travels in a URL: uv reads the registry's credentials from those two variables. The project is private,
# so one of the two ways of signing in is needed; see "Install" in README.md.
#
# uv syntax this relies on (uv 0.5 and later; docs.astral.sh/uv/concepts/indexes/):
#   --index NAME=URL                      an extra package index with a name (`uv tool install --help`);
#   UV_INDEX_<NAME>_USERNAME / _PASSWORD  the credentials of the index called NAME, NAME upper-cased with `-` as `_`
#                                         ("Providing credentials" in the indexes page). The index is searched before
#                                         PyPI, which still serves the dependencies.

set -eu

NAME="dynfarm"
# The index's name decides the variables above: dynfarm -> UV_INDEX_DYNFARM_USERNAME / UV_INDEX_DYNFARM_PASSWORD.
INDEX_NAME="dynfarm"
REGISTRY_URL="https://gitlab.dynapps.be/api/v4/projects/4378/packages/pypi/simple"
GIT_URL="https://gitlab.dynapps.be/infra-es/farm-ai/cli.git"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

say() { printf '%s\n' "$*"; }
fail() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat << USAGE
Usage: sh install.sh [VERSION]

Installs $NAME with uv (installing uv first when it is missing). VERSION is 0.1.0 or v0.1.0; without it, the latest.
With UV_INDEX_DYNFARM_USERNAME and UV_INDEX_DYNFARM_PASSWORD set it installs from the project's package registry,
otherwise from git with your git credentials for gitlab.dynapps.be.
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
# The version ends up in a requirement and in a git ref: accept what a release is called and nothing else.
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
    say "uv is not installed; installing it from $UV_INSTALLER_URL"
    tmp_installer="$(mktemp)"
    # Downloaded to a file first: in a pipe, a failed download would still run whatever half arrived.
    if command -v curl > /dev/null 2>&1; then
        curl -LsSf "$UV_INSTALLER_URL" -o "$tmp_installer" || fail "could not download the uv installer"
    elif command -v wget > /dev/null 2>&1; then
        wget -qO "$tmp_installer" "$UV_INSTALLER_URL" || fail "could not download the uv installer"
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

if [ -n "${UV_INDEX_DYNFARM_USERNAME:-}" ] && [ -n "${UV_INDEX_DYNFARM_PASSWORD:-}" ]; then
    requirement="$NAME"
    [ -z "$version" ] || requirement="$NAME==$version"
    say "Installing $requirement from the project's package registry"
    uv tool install --reinstall --index "$INDEX_NAME=$REGISTRY_URL" "$requirement" ||
        fail "uv could not install $requirement from the registry: check the user name and the token (read_api scope)"
else
    command -v git > /dev/null 2>&1 || fail "git is needed to install without registry credentials (see README.md)"
    if [ -z "$version" ]; then
        # The newest tag by version number, not by date. Asking also proves the person's git credentials work here.
        tags="$(git ls-remote --tags --refs --sort=-v:refname "$GIT_URL" 'v*')" ||
            fail "git could not read $GIT_URL: sign in to gitlab.dynapps.be with git, or set the registry variables"
        # Final releases only (v1.2.3): a pre-release such as v1.3.0rc1 sorts above v1.3.0 and must not be "the latest".
        tag="$(printf '%s\n' "$tags" | grep -E 'refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' | sed -n '1s|.*refs/tags/||p')"
        [ -n "$tag" ] || fail "no release tag found in $GIT_URL"
    else
        tag="v$version"
        git ls-remote --exit-code --tags --refs "$GIT_URL" "refs/tags/$tag" > /dev/null ||
            fail "git could not find the tag $tag in $GIT_URL (no access, or no such release)"
    fi
    say "Installing $NAME $tag from git ($GIT_URL)"
    uv tool install --reinstall "git+$GIT_URL@$tag" || fail "uv could not install $NAME from git"
fi

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
