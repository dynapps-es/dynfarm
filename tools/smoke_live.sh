#!/usr/bin/env bash
# Live smoke of an installed `dynfarm` against a real farm, with a real token, the way a person would use it.
#
#   DYNFARM_SMOKE_TOKEN=... DYNFARM_SMOKE_VALUE=... tools/smoke_live.sh https://farm.example.com
#
# DYNFARM_SMOKE_TOKEN is the token to log in with (piped into `auth login --with-token`, never printed);
# DYNFARM_SMOKE_VALUE is a throw-away secret for the credential round trip. The token goes through the real system
# keyring; the config lives in a temporary directory. Everything the smoke creates is named dynfarm-smoke-<timestamp>
# and deleted by it, also when a step fails midway. No task is launched: that spends money; the e2e suite covers it.
set -u -o pipefail

HOST="${1:?usage: smoke_live.sh FARM_URL}"
: "${DYNFARM_SMOKE_TOKEN:?set DYNFARM_SMOKE_TOKEN (the token to log in with)}"
: "${DYNFARM_SMOKE_VALUE:?set DYNFARM_SMOKE_VALUE (a throw-away secret)}"

# Only what this script decides: no token or farm from the caller's environment may shadow the keyring path.
unset DYNFARM_TOKEN DYNFARM_HOST AGENT_FARM_TOKEN AGENT_FARM_URL CI
DYNFARM_CONFIG_DIR="$(mktemp -d)"
export DYNFARM_CONFIG_DIR
STAMP="$(date -u +%Y%m%d%H%M%S)"
NAME="dynfarm-smoke-${STAMP}"
SCHEDULE_ID=""
CREDENTIAL_SET=""
LOGGED_IN=""

ERR_FILE="$(mktemp)"
step() { # step "description" command...  — LAST_OUTPUT is the command's stdout only (stderr carries notices)
    local what="$1"
    shift
    local out
    if out="$("$@" 2>"$ERR_FILE")"; then
        printf 'ok    %s\n' "$what"
        LAST_OUTPUT="$out"
    else
        printf 'FAIL  %s\n%s\n' "$what" "$out"
        cat "$ERR_FILE"
        exit 1
    fi
}

cleanup() {
    [ -n "$SCHEDULE_ID" ] && dynfarm schedule delete "$SCHEDULE_ID" --yes >/dev/null 2>&1 \
        && echo "cleanup: schedule $SCHEDULE_ID deleted"
    [ -n "$CREDENTIAL_SET" ] && dynfarm credential delete "$NAME" --yes >/dev/null 2>&1 \
        && echo "cleanup: credential $NAME deleted"
    [ -n "$LOGGED_IN" ] && dynfarm auth logout --hostname "$HOST" --yes >/dev/null 2>&1 \
        && echo "cleanup: logged out of $HOST"
    rm -rf "$DYNFARM_CONFIG_DIR" "$ERR_FILE"
}
trap cleanup EXIT

login() { printf '%s\n' "$DYNFARM_SMOKE_TOKEN" | dynfarm auth login --hostname "$HOST" --with-token; }
json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

step "dynfarm --version" dynfarm --version
step "auth login (token through the system keyring)" login
LOGGED_IN=1
step "auth status" dynfarm auth status
case "$LAST_OUTPUT" in *"token from: keyring"*) ;; *) echo "FAIL  auth status: token not from the keyring"; exit 1 ;; esac
step "whoami" dynfarm whoami
step "agent list" dynfarm agent list
step "plugin list" dynfarm plugin list
step "task list --limit 5" dynfarm task list --limit 5
step "credential list" dynfarm credential list
step "schedule list" dynfarm schedule list

step "schedule create (far-future, one run at most)" dynfarm schedule create --json \
    --prompt "dynfarm smoke: this schedule is created, paused and deleted by tools/smoke_live.sh; never run it." \
    --cron "0 3 1 1 *" --timezone UTC --starts-at 2099-01-01T00:00:00Z --max-runs 1
SCHEDULE_ID="$(printf '%s' "$LAST_OUTPUT" | json_field schedule_id)" || { echo "FAIL  no schedule_id in the answer"; exit 1; }
step "schedule pause $SCHEDULE_ID" dynfarm schedule pause "$SCHEDULE_ID"
step "schedule view $SCHEDULE_ID" dynfarm schedule view "$SCHEDULE_ID" --json
case "$(printf '%s' "$LAST_OUTPUT" | json_field state)" in paused) ;; *) echo "FAIL  schedule not paused"; exit 1 ;; esac
step "schedule delete $SCHEDULE_ID" dynfarm schedule delete "$SCHEDULE_ID" --yes
SCHEDULE_ID=""

step "credential set $NAME (from an environment variable)" dynfarm credential set "$NAME" --type generic \
    --description "dynfarm smoke, deleted right away" --value-env DYNFARM_SMOKE_VALUE
CREDENTIAL_SET=1
case "$LAST_OUTPUT" in *"$DYNFARM_SMOKE_VALUE"*) echo "FAIL  the secret was printed"; exit 1 ;; esac
step "credential view $NAME" dynfarm credential view "$NAME"
step "credential delete $NAME" dynfarm credential delete "$NAME" --yes
CREDENTIAL_SET=""

step "nothing named $NAME left" sh -c "! dynfarm schedule list --json | grep -q '$NAME' && ! dynfarm credential list --json | grep -q '$NAME'"
step "auth logout" dynfarm auth logout --hostname "$HOST" --yes
LOGGED_IN=""
echo "smoke: all steps ok against $HOST"
