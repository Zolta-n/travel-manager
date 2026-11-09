#!/bin/sh
# Run once, manually, as root from DSM Task Scheduler. Never enable shell tracing.
set -eu
set +x
umask 077
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/var/packages/ContainerManager/target/usr/bin:/var/packages/Docker/target/usr/bin:/usr/syno/bin:/usr/syno/sbin
export PATH LC_ALL=C
unset ANTHROPIC_API_KEY TRAVEL_PASSWORD TRAVEL_ALLOWED_HOSTS TRAVEL_SECURE_COOKIE TRAVEL_INSTALL_IMAGE TRAVEL_INSTALL_ID COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG
MODE=install
HOST=
REBUILD=false
ROTATE=false
KEY_ACTION=
AI_KEY=
LOCKED=false
STATUS_ENABLED=false
STAGE='Not started'
LOCAL_HEALTH='not verified'
fail() { printf '%s\n' "Refused: $*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
# Synology shared folders can attach inherited ACLs that override chmod (files show as -rwx------+).
# Strip them from every private file the installer creates, then set the mode explicitly.
strip_acl() { if command -v synoacltool >/dev/null 2>&1; then synoacltool -del "$1" >/dev/null 2>&1 || :; fi; }
make_private() { strip_acl "$1"; chmod "$2" "$1" || fail "Cannot set mode $2 on a private installer file (E_PRIVATE_MODE)."; }
valid_ai_key() {
    k=$1
    [ "${#k}" -ge 27 ] && [ "${#k}" -le 306 ] || return 1
    case "$k" in sk-ant-*) ;; *) return 1 ;; esac
    case "$k" in *[!A-Za-z0-9_-]*) return 1 ;; esac
}
write_status() {
    # No raw Docker output, login data, .env contents or password in this file.
    if [ -e private/STATUS.txt ] || [ -L private/STATUS.txt ]; then
        safe_file private/STATUS.txt
        [ "$(stat -c %a private/STATUS.txt)" = 600 ] || return 1
        [ "$(sed -n '1p' private/STATUS.txt)" = 'Travel Manager installer status v2' ] || return 1
        [ "$(sed -n '2p' private/STATUS.txt)" = "Installation: $ID" ] || return 1
    fi
    printf '%s\n' 'Travel Manager installer status v2' "Installation: $ID" "Stage: $STAGE" "Local container: $LOCAL_HEALTH" 'iPhone/remote HTTPS/VPN/certificate: NOT VERIFIED; configure separately.' > private/STATUS.txt
    strip_acl private/STATUS.txt
    chmod 600 private/STATUS.txt
    chown "$PACKAGE_UID" private/STATUS.txt
}
cleanup() {
    result=$?
    trap - 0
    if [ "$STATUS_ENABLED" = true ] && [ "$result" -ne 0 ]; then
        STAGE="FAILED or interrupted during $STAGE; inspect private Container Manager status/logs and rerun."
        write_status >/dev/null 2>&1 || :
    fi
    if [ "$LOCKED" = true ]; then rmdir "$DIR/.install.lock" || :; fi
    exit "$result"
}
usage() {
    note 'Usage: /bin/sh /absolute/app/install.sh [--check|--plan] [--host exact.https.hostname[:port]] [--build|--pull]'
    note '--build / --pull   download the latest tested app image from GitHub now (normally Watchtower does this).'
    note '       [--rotate-password] [--set-ai-key | --remove-ai-key]'
    note '--rotate-password  new random password in .env and private/LOGIN.txt; every device must sign in again.'
    note '--set-ai-key       reads private/ANTHROPIC_API_KEY.txt (only the key), stores it in .env, deletes the text file.'
    note '--remove-ai-key    removes the Anthropic key; documents are then read by the local NAS readers only.'
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --check|--plan) MODE=check; shift ;;
        --host) [ "$#" -ge 2 ] && [ -z "$HOST" ] || fail '--host requires one exact hostname.'; HOST=$2; shift 2 ;;
        --build|--pull) REBUILD=true; shift ;;
        --rotate-password) ROTATE=true; shift ;;
        --set-ai-key) [ -z "$KEY_ACTION" ] || fail 'Use only one AI key option.'; KEY_ACTION=set; shift ;;
        --remove-ai-key) [ -z "$KEY_ACTION" ] || fail 'Use only one AI key option.'; KEY_ACTION=remove; shift ;;
        --help) usage; exit 0 ;;
        *) fail 'Unknown argument. Use --help.' ;;
    esac
done
validate_host() {
    h=$1
    [ "${#h}" -le 259 ] && [ -n "$h" ] || return 1
    case "$h" in *[!a-z0-9.:-]*|*:*:*) return 1 ;; esac
    name=${h%%:*}
    case "$h" in
        *:*) port=${h#*:}; case "$port" in ''|0*|*[!0-9]*) return 1 ;; esac
             [ "${#port}" -le 5 ] && [ "$port" -le 65535 ] || return 1 ;;
    esac
    [ "${#name}" -le 253 ] || return 1
    printf '%s\n' "$name" | grep -Eq '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' || return 1
    oldifs=$IFS; IFS=.; set -- $name; IFS=$oldifs
    for label do
        [ -n "$label" ] && [ "${#label}" -le 63 ] || return 1
        case "$label" in -*|*-) return 1 ;; esac
    done
    case "$name" in *..*) return 1 ;; esac
}
[ -z "$HOST" ] || validate_host "$HOST" || fail 'Use a lowercase DNS hostname (or IPv4), optionally :1-65535; no URL, whitespace, wildcard, userinfo, shell syntax or IPv6.'
case "$0" in /*/install.sh) DIR=${0%/*} ;; *) fail 'Invoke install.sh by its absolute path.' ;; esac
case "$DIR" in *','*|*':'*|*'
'*) fail 'The app path cannot contain commas, colons or newlines.' ;; esac
[ -d "$DIR" ] && [ ! -L "$DIR" ] || fail 'App folder is not a real directory.'
PHYSICAL=$(CDPATH= cd -P "$DIR" && pwd -P) || fail 'Cannot resolve app folder.'
[ "$PHYSICAL" = "$DIR" ] || fail 'Use the physical app path, without symlinks, . or .. components.'
cd "$DIR"
PACKAGE_UID=$(stat -c %u "$DIR") || fail 'stat is required.'
# Only root and the existing trusted uploader may own the app/ancestor directories.
check_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] || fail 'A package/state/data directory is a link or not a directory.'
    owner=$(stat -c %u "$1"); mode=$(stat -c %a "$1")
    [ "$owner" = 0 ] || [ "$owner" = "$PACKAGE_UID" ] || fail 'Untrusted directory owner.'
    [ "$((0$mode & 022))" -eq 0 ] || fail 'App and ancestor directories must not be group/world writable; use a private admin-only folder.'
}
p=$DIR
while :; do check_dir "$p"; [ "$p" = / ] && break; p=${p%/*}; [ -n "$p" ] || p=/; done
safe_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %h "$1")" = 1 ] || fail 'Required file is missing, linked, or not a regular single-link file.'
    owner=$(stat -c %u "$1"); mode=$(stat -c %a "$1")
    [ "$owner" = 0 ] || [ "$owner" = "$PACKAGE_UID" ] || fail 'Untrusted file owner.'
    [ "$((0$mode & 022))" -eq 0 ] || fail 'Package/config files cannot be group/world writable.'
}
private_file() {
    safe_file "$1"
    [ "$(stat -c %a "$1")" = 600 ] || fail 'Existing private files must already have mode 0600; refusing to change them.'
    [ "$(stat -c %u "$1")" = "$(id -u)" ] || fail 'Existing installer state and .env must belong to root.'
}
for d in private web; do check_dir "$DIR/$d"; done
# File Station extraction into an ACL share can leave package files group/world writable.
# As root, repair only the fixed package allowlist below (owned by root or the uploader), never data or config.
if [ "$MODE" = install ] && [ "$(id -u)" = 0 ]; then
    for f in .easy-package PACKAGE.sha256 install.sh Dockerfile .dockerignore compose.yaml easy_data.py ai_reader.py app.py store.py extractor.py reader_worker.py server.py initialize_data.py requirements.txt web/index.html private/seed.json; do
        [ -f "$f" ] && [ ! -L "$f" ] || continue
        fowner=$(stat -c %u "$f"); fmode=$(stat -c %a "$f")
        [ "$fowner" = 0 ] || [ "$fowner" = "$PACKAGE_UID" ] || continue
        if [ "$((0$fmode & 022))" -ne 0 ]; then
            strip_acl "$f"
            chmod 600 "$f" || fail "Cannot repair permissions of $f (E_PRIVATE_MODE)."
            note "Repaired permissions of $f (File Station extraction)."
        fi
    done
fi
for f in .easy-package PACKAGE.sha256 install.sh Dockerfile .dockerignore compose.yaml easy_data.py ai_reader.py app.py store.py extractor.py reader_worker.py server.py initialize_data.py requirements.txt web/index.html private/seed.json; do safe_file "$DIR/$f"; done
[ "$(cat .easy-package)" = travel-manager-nas-easy-v2 ] || fail 'Wrong package marker.'
# The fixed path allowlist also prevents a modified manifest from reading outside this package.
count=0
while IFS= read -r line; do
    hash=${line%%  *}; file=${line#*  }
    [ "${#hash}" = 64 ] || fail 'Invalid package manifest.'
    case "$hash" in *[!a-f0-9]*) fail 'Invalid package digest.' ;; esac
    case "$file" in .easy-package|install.sh|Dockerfile|.dockerignore|compose.yaml|easy_data.py|ai_reader.py|app.py|store.py|extractor.py|reader_worker.py|server.py|initialize_data.py|requirements.txt|web/index.html|private/seed.json) ;; *) fail 'Invalid package manifest path.' ;; esac
    [ "$(sha256sum "$file" | cut -c 1-64)" = "$hash" ] || fail 'Package integrity check failed. Re-extract the trusted complete ZIP.'
    count=$((count + 1))
done < PACKAGE.sha256
[ "$count" = 16 ] || fail 'Incomplete package manifest.'
# Independently verify all 16 required names occur exactly once.
for f in .easy-package install.sh Dockerfile .dockerignore compose.yaml easy_data.py ai_reader.py app.py store.py extractor.py reader_worker.py server.py initialize_data.py requirements.txt web/index.html private/seed.json; do
    [ "$(grep -Fc "  $f" PACKAGE.sha256)" = 1 ] || fail 'Incomplete or duplicate package manifest.'
done
[ "$(id -u)" = 0 ] || [ "$MODE" = check ] || fail 'Select root for this manually-run DSM task; see START_HERE.md.'
if [ -e .installer ] || [ -L .installer ]; then
    check_dir "$DIR/.installer"
    [ "$(stat -c %a .installer)" = 700 ] || fail 'Installer state directory must be 0700.'
fi
if [ -e .install.lock ] || [ -L .install.lock ]; then fail 'Another install holds .install.lock (or a previous task was interrupted). Do not remove it until DSM confirms that task has stopped; see INSTALLER_DETAILS.md.'; fi
DOCKER=$(command -v docker || true)
[ -n "$DOCKER" ] && [ -x "$DOCKER" ] || fail 'Docker CLI not found. Install/start Synology Container Manager (Docker on supported older DSM) first.'
TIMEOUT=$(command -v timeout || true)
[ -n "$TIMEOUT" ] || fail 'A system timeout utility is required for bounded Docker readiness/health checks.'
"$TIMEOUT" 15 "$DOCKER" info >/dev/null 2>&1 || fail 'Docker daemon is not ready or is inaccessible. Start Container Manager and retry.'
COMPOSE=
if "$DOCKER" compose version >/dev/null 2>&1; then
    COMPOSE=plugin
else
    COMPOSE=$(command -v docker-compose || true)
    if [ -z "$COMPOSE" ]; then
        for candidate in /var/packages/ContainerManager/target/usr/bin/docker-compose /var/packages/Docker/target/usr/bin/docker-compose; do
            if [ -x "$candidate" ]; then COMPOSE=$candidate; break; fi
        done
    fi
    [ -n "$COMPOSE" ] && "$COMPOSE" version >/dev/null 2>&1 || fail 'A working Docker Compose v2 plugin or docker-compose is required.'
fi
compose() {
    if [ "$COMPOSE" = plugin ]; then "$DOCKER" compose --project-directory "$DIR" --env-file "$DIR/.env" --project-name "$PROJECT" --file "$DIR/compose.yaml" "$@";
    else "$COMPOSE" --project-directory "$DIR" --env-file "$DIR/.env" --project-name "$PROJECT" --file "$DIR/compose.yaml" "$@"; fi
}
if [ "$MODE" = install ]; then
    mkdir -m 700 .install.lock 2>/dev/null || fail 'Install lock is busy.'
    LOCKED=true
    trap cleanup 0
    trap 'exit 130' INT
    trap 'exit 143' TERM
fi
if [ -e .installer/id ] || [ -L .installer/id ]; then
    private_file .installer/id
    ID=$(cat .installer/id)
    [ "${#ID}" = 24 ] || fail 'Invalid installer identity.'
    case "$ID" in *[!a-f0-9]*) fail 'Invalid installer identity.' ;; esac
else
    [ ! -e .env ] && [ ! -L .env ] && [ ! -e private/LOGIN.txt ] && [ ! -L private/LOGIN.txt ] && [ ! -e private/STATUS.txt ] && [ ! -L private/STATUS.txt ] || fail 'Unmanaged config/credentials/status exist. Refusing to adopt or overwrite them.'
    ID=
fi
if [ -e data ] || [ -L data ]; then
    [ -d data ] && [ ! -L data ] || fail 'data must be a real app-local directory.'
    if [ -n "$(find data -mindepth 1 -maxdepth 1 -print)" ]; then
        [ -n "$ID" ] || fail 'Unmanaged data is not empty. Use a freshly extracted folder; migration is separate.'
        safe_file data/.easy-install-id
        [ "$(cat data/.easy-install-id)" = "$ID" ] || fail 'Data identity differs; no adoption or migration.'
        data_owner=$(stat -c %u data)
        [ "$data_owner" = 0 ] || [ "$data_owner" = 10001 ] || fail 'Unexpected managed data owner; refusing automatic repair.'
    fi
fi
# Existing .env is parsed as data, never sourced or evaluated. Only our exact grammar is accepted.
if [ -e .env ] || [ -L .env ]; then
    private_file .env
    ENV_LINES=$(wc -l < .env | tr -d ' ')
    [ "$ENV_LINES" = 5 ] || [ "$ENV_LINES" = 6 ] || fail 'Unrecognized .env format; no changes made.'
    [ "$(sed -n '1p' .env)" = '# Managed by travel-manager-nas-easy-v2; keep private.' ] || fail 'Unmanaged .env; no changes made.'
    PASSWORD=$(sed -n "2s/^TRAVEL_PASSWORD='\([A-Za-z0-9!_-]*\)'$/\1/p" .env)
    [ "${#PASSWORD}" -ge 48 ] && [ "${#PASSWORD}" -le 128 ] || fail 'Existing password is weak or unsupported; refusing to replace it.'
    classes=0
    case "$PASSWORD" in *[a-z]*) classes=$((classes + 1)) ;; esac
    case "$PASSWORD" in *[A-Z]*) classes=$((classes + 1)) ;; esac
    case "$PASSWORD" in *[0-9]*) classes=$((classes + 1)) ;; esac
    case "$PASSWORD" in *[!a-zA-Z0-9]*) classes=$((classes + 1)) ;; esac
    [ "$classes" -ge 3 ] || fail 'Existing password is weak; refusing to replace it.'
    CONFIG_HOST=$(sed -n "3s/^TRAVEL_ALLOWED_HOSTS='\([^']*\)'$/\1/p" .env)
    validate_host "$CONFIG_HOST" || fail 'Invalid existing host; no changes made.'
    [ -z "$HOST" ] || [ "$HOST" = "$CONFIG_HOST" ] || fail 'Host differs from existing .env; reruns never change the host/password.'
    HOST=$CONFIG_HOST
    [ "$(sed -n '4p' .env)" = TRAVEL_SECURE_COOKIE=true ] || fail 'Secure cookies must remain true; no changes made.'
    [ "$(sed -n '5p' .env)" = "TRAVEL_INSTALL_ID=$ID" ] || fail 'Config belongs to another installer identity.'
    if [ "$ENV_LINES" = 6 ]; then
        AI_KEY=$(sed -n "6s/^ANTHROPIC_API_KEY='\(sk-ant-[A-Za-z0-9_-]*\)'$/\1/p" .env)
        valid_ai_key "$AI_KEY" || fail 'Unrecognized AI key line in .env; no changes made.'
    fi
else
    [ -n "$HOST" ] || fail '--host exact.https.hostname[:port] is required on first install (no defaults).'
    PASSWORD=
fi
if [ -e private/LOGIN.txt ] || [ -L private/LOGIN.txt ]; then
    safe_file private/LOGIN.txt
    [ "$(stat -c %a private/LOGIN.txt)" = 600 ] && [ "$(stat -c %u private/LOGIN.txt)" = "$PACKAGE_UID" ] || fail 'Credentials must be 0600 and owned only by the original package owner.'
    [ -n "$PASSWORD" ] || fail 'Credentials exist without a managed .env.'
    [ "$(wc -l < private/LOGIN.txt | tr -d ' ')" = 3 ] && [ "$(sed -n '1p' private/LOGIN.txt)" = 'Travel Manager private login (no username)' ] && [ "$(sed -n '2p' private/LOGIN.txt)" = "HTTPS: https://$HOST" ] && [ "$(sed -n '3p' private/LOGIN.txt)" = "Password: $PASSWORD" ] || fail 'Credentials disagree with .env; refusing to overwrite.'
fi
if [ -e private/STATUS.txt ] || [ -L private/STATUS.txt ]; then
    safe_file private/STATUS.txt
    [ "$(stat -c %a private/STATUS.txt)" = 600 ] && [ "$(sed -n '1p' private/STATUS.txt)" = 'Travel Manager installer status v2' ] && [ "$(sed -n '2p' private/STATUS.txt)" = "Installation: $ID" ] || fail 'Unmanaged or unsafe status file; refusing to overwrite.'
fi
if [ "$ROTATE" = true ] || [ -n "$KEY_ACTION" ]; then
    [ "$MODE" = install ] || fail '--rotate-password and the AI key options cannot be combined with --check/--plan.'
    [ -n "$PASSWORD" ] || fail 'Install normally first; --rotate-password and the AI key options change an existing installation.'
fi
KEY_FILE=private/ANTHROPIC_API_KEY.txt
if [ "$KEY_ACTION" = set ]; then
    [ -e "$KEY_FILE" ] || [ -L "$KEY_FILE" ] || fail 'Create private/ANTHROPIC_API_KEY.txt containing only your Anthropic API key, then rerun with --set-ai-key.'
    [ -f "$KEY_FILE" ] && [ ! -L "$KEY_FILE" ] && [ "$(stat -c %h "$KEY_FILE")" = 1 ] || fail 'private/ANTHROPIC_API_KEY.txt must be a regular file.'
    key_owner=$(stat -c %u "$KEY_FILE")
    [ "$key_owner" = 0 ] || [ "$key_owner" = "$PACKAGE_UID" ] || fail 'private/ANTHROPIC_API_KEY.txt has an untrusted owner.'
    [ "$(wc -c < "$KEY_FILE" | tr -d ' ')" -le 512 ] || fail 'private/ANTHROPIC_API_KEY.txt must contain only the key.'
    NEW_AI_KEY=$(tr -d ' \t\r\n' < "$KEY_FILE")
    valid_ai_key "$NEW_AI_KEY" || fail 'private/ANTHROPIC_API_KEY.txt does not contain a valid Anthropic API key (it starts with sk-ant-). Nothing was changed.'
fi
if [ "$MODE" = install ] && [ -z "$ID" ]; then
    if [ ! -d .installer ]; then mkdir -m 700 .installer; fi
    make_private .installer 700
    ID=$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#ID}" = 24 ] || fail 'A working /dev/urandom and od are required.'
    (set -C; printf '%s\n' "$ID" > .installer/id) || fail 'Cannot create installer identity.'
    make_private .installer/id 600
fi
PROJECT=tme$ID
# The app image is built and tested by GitHub Actions and published to ghcr.io; Watchtower keeps it current.
IMAGE=ghcr.io/zolta-n/travel-manager:latest
IMAGE_SOURCE=https://github.com/Zolta-n/travel-manager
LEGACY_IMAGE=travel-manager-easy-$ID:local
inspect() { "$DOCKER" inspect --format "$1" "$2" 2>/dev/null; }
owned() {
    [ "$(inspect '{{index .Config.Labels "org.travel-manager.easy.install"}}' "$1")" = "$ID" ] &&
    [ "$(inspect '{{index .Config.Labels "com.docker.compose.project"}}' "$1")" = "$PROJECT" ] &&
    [ "$(inspect '{{index .Config.Labels "com.docker.compose.service"}}' "$1")" = travel-manager ] &&
    [ "$(inspect '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$1")" = "$DIR" ] &&
    [ "$(inspect '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$1")" = "$DIR/compose.yaml" ] &&
    case "$(inspect '{{.Config.Image}}' "$1")" in "$IMAGE"|"$LEGACY_IMAGE") ;; *) return 1 ;; esac
}
OWN_RUNNING=false
if [ -n "$ID" ]; then
    project_ids=$("$DOCKER" ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.ID}}') || fail 'Cannot inspect existing project containers.'
    for cid in $project_ids; do owned "$cid" || fail 'Project label collision with an unrelated container; nothing will be replaced.'; done
    for name in "$PROJECT-travel-manager-1" "${PROJECT}_travel-manager_1"; do
        if "$DOCKER" container inspect "$name" >/dev/null 2>&1; then owned "$name" || fail 'Container name collision; unrelated containers are never touched.'; fi
    done
fi
all_ids=$("$DOCKER" ps --format '{{.ID}}') || fail 'Cannot inspect Docker port ownership.'
for cid in $all_ids; do
    bindings=$(inspect '{{range $port,$bindings := .HostConfig.PortBindings}}{{range $bindings}}{{println $port .HostIp .HostPort}}{{end}}{{end}}' "$cid") || fail 'Cannot inspect container port bindings.'
    if printf '%s\n' "$bindings" | grep -Eq '(^|[[:space:]])8787$'; then
        [ -n "$ID" ] && owned "$cid" || fail 'Port 8787 is published by another container (possibly v1). Choose a different NAS or stop/reconfigure that app separately; no automatic migration.'
        [ "$bindings" = '8787/tcp 127.0.0.1 8787' ] || fail 'Managed service has unexpected/public port bindings; refusing to alter it.'
        OWN_RUNNING=true
    fi
done
if [ "$OWN_RUNNING" != true ]; then
    [ -r /proc/net/tcp ] || fail 'Cannot safely check local TCP port ownership on this NAS.'
    TCP_TABLES=/proc/net/tcp
    # /proc/net/tcp6 is absent when IPv6 is disabled; it must be readable when present.
    if [ -e /proc/net/tcp6 ]; then
        [ -r /proc/net/tcp6 ] || fail 'Cannot safely check local TCP port ownership on this NAS.'
        TCP_TABLES="/proc/net/tcp /proc/net/tcp6"
    fi
    # shellcheck disable=SC2086
    if awk '$4 == "0A" && $2 ~ /:2253$/ { found=1 } END { exit !found }' $TCP_TABLES; then fail 'TCP port 8787 is already listening locally; no existing service will be changed.'; fi
fi
HAVE_IMAGE=false
if [ -n "$ID" ] && "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
    [ "$("$DOCKER" image inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$IMAGE" 2>/dev/null)" = "$IMAGE_SOURCE" ] || fail 'The local ghcr.io/zolta-n/travel-manager image is not from the expected repository; nothing was replaced.'
    HAVE_IMAGE=true
fi
if [ "$MODE" = check ]; then
    note 'CHECK ONLY: package/path checks, daemon, Compose and port preflight passed. Nothing created or started.'
    note "Configured HTTPS authority: $HOST (HTTPS/reverse proxy itself is NOT checked)."
    note 'Install will create private config/credentials, download or reuse the tested app image from GitHub, prepare only ./data, start one loopback-only service, and wait at most 120 seconds for local health.'
    note 'No NAS/network/HTTPS settings or existing apps will be changed. The image download and OCR runtime remain untested by this check.'
    exit 0
fi
STATUS_ENABLED=true
STAGE='Preparing private config and credentials'
write_status || fail 'Cannot write the private nonsecret status file.'
new_password() {
    random=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#random}" = 64 ] || fail 'Secure random generation failed.'
    PASSWORD=TM!a$random
}
write_env() {
    (set -C
     printf '%s\n' '# Managed by travel-manager-nas-easy-v2; keep private.' "TRAVEL_PASSWORD='$PASSWORD'" "TRAVEL_ALLOWED_HOSTS='$HOST'" TRAVEL_SECURE_COOKIE=true "TRAVEL_INSTALL_ID=$ID" > "$1"
     if [ -n "$AI_KEY" ]; then printf '%s\n' "ANTHROPIC_API_KEY='$AI_KEY'" >> "$1"; fi) || fail 'Cannot create .env; no existing config was replaced.'
    make_private "$1" 600
}
write_login() {
    (set -C; printf '%s\n' 'Travel Manager private login (no username)' "HTTPS: https://$HOST" "Password: $PASSWORD" > "$1") || fail 'Cannot create private credentials.'
    make_private "$1" 600
    chown "$PACKAGE_UID" "$1" || fail 'Cannot assign credentials to the original package owner.'
}
replace_private() {
    # Write a complete new copy next to the original, then rename it over the original.
    if [ -e "$1.new" ] || [ -L "$1.new" ]; then safe_file "$1.new"; rm -f "$1.new"; fi
    "$2" "$1.new"
    mv -f "$1.new" "$1" || fail 'Cannot replace a private file; the previous version is unchanged.'
}
if [ -z "$PASSWORD" ]; then
    new_password
    write_env .env
elif [ "$ROTATE" = true ] || [ -n "$KEY_ACTION" ]; then
    STAGE='Updating private configuration'; write_status
    if [ "$ROTATE" = true ]; then new_password; fi
    if [ "$KEY_ACTION" = set ]; then AI_KEY=$NEW_AI_KEY; fi
    if [ "$KEY_ACTION" = remove ]; then AI_KEY=; fi
    replace_private .env write_env
    if [ "$ROTATE" = true ]; then
        replace_private private/LOGIN.txt write_login
        note 'Password rotated: the new password is in private/LOGIN.txt. Every device must sign in again.'
    fi
    if [ "$KEY_ACTION" = set ]; then
        rm -f "$KEY_FILE" || note 'WARNING: could not delete private/ANTHROPIC_API_KEY.txt; delete it in File Station.'
        note 'Anthropic API key stored in the root-only .env; private/ANTHROPIC_API_KEY.txt was deleted.'
    fi
    if [ "$KEY_ACTION" = remove ]; then note 'Anthropic API key removed. Documents will be read by the local NAS readers only.'; fi
fi
if [ ! -e private/LOGIN.txt ]; then write_login private/LOGIN.txt; fi
# Never emit subprocess output: even Compose errors can contain interpolated secrets.
if [ "$HAVE_IMAGE" != true ] || [ "$REBUILD" = true ]; then
    STAGE='Downloading the tested app image'; write_status
    note 'Downloading the tested app image from GitHub (ghcr.io). Output is suppressed to keep task logs private.'
    "$DOCKER" pull "$IMAGE" >/dev/null 2>&1 || fail 'Could not download the app image from ghcr.io (E_IMAGE_PULL). Check the NAS internet connection and that the GitHub package is public; private config is retained.'
fi
[ "$("$DOCKER" image inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$IMAGE" 2>/dev/null)" = "$IMAGE_SOURCE" ] || fail 'Downloaded image identity check failed (E_IMAGE_SOURCE).'
# The app runs as UID 10001: confirm it can read every program file before touching data.
image_check=$("$DOCKER" run --rm -i --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 16 --memory 128m --entrypoint python "$IMAGE" -c 'import os; bad=[1 for d,_,fs in os.walk("/app") for f in fs if not os.access(os.path.join(d,f),os.R_OK)]; print("E_APP_UNREADABLE" if bad or not os.access("/app/app.py",os.R_OK) else "OK")' < /dev/null 2>/dev/null) || image_check=E_IMAGE_CHECK_FAILED
[ "$image_check" = OK ] || fail "The image cannot read its own program files (reason ${image_check:-E_IMAGE_CHECK_FAILED}). Rerun this task with --pull."
if [ ! -d data ]; then mkdir -m 700 data; fi
[ -d data ] && [ ! -L data ] || fail 'Unsafe data directory.'
DATA_MODE=$(stat -c %a data)
[ "$((0$DATA_MODE & 022))" -eq 0 ] || fail 'data must not be writable by groups/Everyone.'
seed_hash=$(sha256sum private/seed.json | cut -c 1-64)
# Only a fixed E_* code is ever taken from helper output; raw Docker/helper text is never printed.
reason_from() {
    code=$(printf '%s\n' "$1" | sed -n 's/^REASON: \(E_[A-Z_]*\)$/\1/p' | head -n 1)
    if [ -z "$code" ]; then case "$1" in *'Permission denied'*) code=E_APP_UNREADABLE ;; *) code=E_UNKNOWN ;; esac; fi
    printf '%s' "$code"
}
STAGE='Preparing/validating app data'; write_status
note 'Preparing only this app data folder; existing managed database/originals are not rewritten.'
data_owner=$(stat -c %u data)
if [ "$data_owner" != 10001 ]; then
    # A pre-created empty uploader-owned ./data is the only host-side chown.
    # Never take ownership of a nonempty uploader-owned/unmanaged directory.
    if [ "$data_owner" != 0 ]; then
        [ -z "$(find data -mindepth 1 -maxdepth 1 -print)" ] || fail 'Refusing ownership changes on nonempty unmanaged data.'
        chown 0:0 data
        chmod 700 data
    fi
    if ! helper_errors=$("$DOCKER" run --rm -i --network none --read-only --user 0:0 --cap-drop ALL --cap-add CHOWN --cap-add FOWNER --security-opt no-new-privileges:true --pids-limit 32 --memory 256m --mount "type=bind,src=$DIR/data,dst=/data" --entrypoint python "$IMAGE" /app/easy_data.py --project "$ID" --seed-sha256 "$seed_hash" < private/seed.json 2>&1 >/dev/null); then
        fail "Fresh data preparation refused or failed (reason $(reason_from "$helper_errors")). No recursive permissions were changed. See INSTALLER_DETAILS.md; resolve the issue and rerun."
    fi
fi
# Reruns only validate as the app UID: root has no DAC override, and never
# traverses or repairs an active managed database. Only ./data is mounted.
if ! helper_errors=$("$DOCKER" run --rm -i --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 --memory 256m --mount "type=bind,src=$DIR/data,dst=/data" --entrypoint python "$IMAGE" /app/easy_data.py --project "$ID" --seed-sha256 "$seed_hash" --validate < private/seed.json 2>&1 >/dev/null); then
    fail "Managed data validation refused (reason $(reason_from "$helper_errors")). Existing seed/database/originals are not repaired or replaced. See INSTALLER_DETAILS.md for this reason code."
fi
compose config --quiet >/dev/null 2>&1 || fail 'Compose configuration validation failed; output withheld to protect credentials.'
STAGE='Starting app and checking local health'; write_status
note 'Starting the single non-root app, bound only to 127.0.0.1:8787.'
compose up --detach --no-build --no-deps travel-manager >/dev/null 2>&1 || fail 'Compose start failed. Inspect the project in Container Manager (private local logs); never share environment/config dumps. Fix the cause and rerun; data, host and password are retained.'
cid=$(compose ps -q travel-manager 2>/dev/null) || fail 'Cannot obtain started service ID.'
[ -n "$cid" ] && owned "$cid" || fail 'Started service ownership could not be verified.'
deadline=$(($(date +%s) + 120))
while [ "$(date +%s)" -lt "$deadline" ]; do
    remaining=$((deadline - $(date +%s)))
    [ "$remaining" -gt 0 ] || break
    status=$("$TIMEOUT" "$remaining" "$DOCKER" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$cid" 2>/dev/null) || fail 'Container disappeared or Docker health inspection exceeded the bounded wait.'
    if [ "$status" = healthy ]; then
        LOCAL_HEALTH='healthy at 127.0.0.1:8787 (local only)'
        STAGE='Completed local installation'; write_status
        note 'SUCCESS: server is healthy locally at 127.0.0.1:8787. This does not verify external HTTPS, OCR accuracy or remote access.'
        note "Read private/LOGIN.txt privately as the account owning this uploaded folder (UID $PACKAGE_UID). The password is never printed in task output."
        if [ -n "$AI_KEY" ]; then note 'AI reading: Anthropic API key configured (documents are sent to Claude for reading).'; else note 'AI reading: no Anthropic API key; local NAS readers only. See START_HERE.md, --set-ai-key.'; fi
        [ "$PACKAGE_UID" != 0 ] || note 'WARNING: package owner is root. File Station retrieval is not guaranteed; a verified private root-file retrieval method is required. Do not expose the password in task logs/email.'
        exit 0
    fi
    remaining=$((deadline - $(date +%s)))
    [ "$remaining" -gt 0 ] || break
    running=$("$TIMEOUT" "$remaining" "$DOCKER" inspect --format '{{.State.Running}}' "$cid" 2>/dev/null) || fail 'Container status unavailable within the bounded wait.'
    [ "$running" = true ] || fail 'App exited before becoming healthy. Inspect private local Container Manager logs; rerun after fixing the cause.'
    [ "$(date +%s)" -lt "$deadline" ] && sleep 2
done
fail 'Local health did not become healthy within 120 seconds. The app may still be starting; inspect Container Manager status/private local logs and rerun the same task. External HTTPS has not been checked.'
