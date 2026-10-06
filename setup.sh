#!/bin/sh
# Sets up Shijhon in front of its own Navidrome with Docker Compose: the steps of
# docs/quick-start.md ("By hand"), done for you. From a checkout:
#
#   ./setup.sh
#
# It asks where your music is, and a username and a password for your own account;
# ./setup.sh --help lists the options that answer instead. The databases and caches are
# kept in Docker volumes.
#
# An existing configuration, password file or Navidrome database is kept, an existing
# packaging/.env is used, and the run goes on from what is there.
#
# Passwords are never arguments of a command: they are typed without echo or read from a
# file, and reach Navidrome on the standard input of a command inside its container.
set -eu
folder_umask=$(umask)
umask 077
# Nothing inherited from the caller may show a secret: a variable that arrives exported
# stays exported when it is assigned to, and would travel on to every command.
unset own_password service_password token answer nd_answer nd_body
unset -f printf read 2>/dev/null || :

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
packaging=$here/packaging
env_file=$packaging/.env
config_dir=$packaging/config
config_file=$config_dir/shijhon.toml
password_file=$config_dir/navidrome-password
service_user=shijhon
default_port=4533
newline='
'

usage() {
    cat <<EOF
Usage: ./setup.sh [options]

Sets up Navidrome and Shijhon with Docker Compose (docs/quick-start.md), with their
databases and caches in Docker volumes. It asks for what it needs. Each answer can be
given as an option instead:

  --music DIR            your music folder
  --user NAME            the username of your own account
  --password-file FILE   a file whose first line is that account's password
                         ("-": standard input)
  --port N               the port your apps connect to (default: $default_port)
  --adapters "SPEC ..."  catalog adapters to install (docs/catalogs.md)
  --no-questions         ask nothing: use the options, and the defaults for the rest
  -h, --help             this text

Without a terminal it asks nothing either. Run again, it goes on from what is there.
EOF
}

# --- messages, and what to say when the run stops ---------------------------------------

done_so_far=""
tmp_file=""
tty_state=""
made_password_file=false

say() { printf '%s\n' "$*"; }

did() { done_so_far="$done_so_far  - $*$newline"; }

fail() {
    printf 'setup: %s\n' "$*" >&2
    exit 1
}

restore() {
    if [ -n "$tty_state" ]; then
        stty "$tty_state" 2>/dev/null || :
        tty_state=""
    fi
    if [ -n "$tmp_file" ]; then
        rm -f "$tmp_file"
        tmp_file=""
    fi
}

on_exit() {
    status=$?
    restore
    if [ "$status" -ne 0 ] && [ -n "$done_so_far" ]; then
        {
            printf '\nThe setup did not finish. Done so far:\n%s' "$done_so_far"
            printf 'Run ./setup.sh again: it goes on from there.\n'
        } >&2
    fi
}

on_signal() {
    printf '\nsetup: interrupted\n' >&2
    exit 130
}

trap on_exit EXIT
trap on_signal INT TERM HUP

# --- options -----------------------------------------------------------------------------

music=""
own_user=""
own_password=""
service_password=""
own_password_file=""
port=""
adapters=""
adapters_given=false
questions=true

need_value() {
    [ "$#" -ge 2 ] || fail "$1 needs a value (./setup.sh --help)"
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --music) need_value "$@"; music=$2; shift 2 ;;
        --user) need_value "$@"; own_user=$2; shift 2 ;;
        --password-file) need_value "$@"; own_password_file=$2; shift 2 ;;
        --port) need_value "$@"; port=$2; shift 2 ;;
        --adapters) need_value "$@"; adapters=$2; adapters_given=true; shift 2 ;;
        --no-questions) questions=false; shift ;;
        -h | --help) usage; exit 0 ;;
        *) fail "unknown option: $1 (./setup.sh --help)" ;;
    esac
done

# Questions only at a terminal; and never when the password comes on the standard input.
if [ ! -t 0 ] || [ "$own_password_file" = "-" ]; then
    questions=false
fi

# --- small tools -------------------------------------------------------------------------

compose() {
    (cd "$packaging" && docker compose "$@")
}

# The text with "\" and '"' escaped: a JSON string's content, and a quoted value of a curl
# configuration file (the same two characters need it in both).
escaped() {
    printf '%s' "$1" | LC_ALL=C sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

has_control() {
    case $1 in *[[:cntrl:]]*) return 0 ;; esac
    return 1
}

# A folder's path as Compose needs it: absolute, without the characters its files give a
# meaning to. The folder exists.
resolved() {
    (CDPATH='' cd -- "$1" && pwd -P)
}

path_problem() {
    case $1 in
        *"$newline"* | *:* | *\\*) say "a path with a colon, a backslash or a line break cannot be used with Compose" ;;
        *) return 1 ;;
    esac
}

# "~/Music" as typed at a question.
home_expanded() {
    case $1 in
        \~) printf '%s' "$HOME" ;;
        \~/*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
        *) printf '%s' "$1" ;;
    esac
}

# A value as one line of a Compose .env file: single-quoted, so nothing in it is expanded.
env_line() {
    printf "%s='%s'\n" "$1" "$(printf '%s' "$2" | LC_ALL=C sed "s/'/\\\\'/g")"
}

# The value of a variable in packaging/.env as Compose reads it ("NAME=value", also with
# "export", spaces around "=", or ":"), for a bare value or one in single quotes. Fails for
# a value it cannot read with certainty (double quotes, "$").
env_value() {
    line=$(LC_ALL=C sed -n "s/^[[:space:]]*\\(export[[:space:]]\\{1,\\}\\)\\{0,1\\}$1[[:space:]]*[=:][[:space:]]*//p" "$env_file") ||
        return 1
    line=$(printf '%s\n' "$line" | tail -n 1) || return 1
    case $line in
        "'"*"'")
            line=${line#"'"}
            line=${line%"'"}
            printf '%s' "$line" | LC_ALL=C sed "s/\\\\'/'/g"
            ;;
        *"'"* | *'"'* | *'$'* | *\\* | *'`'*) return 1 ;;
        *) printf '%s' "$line" | LC_ALL=C sed -e 's/[[:space:]]#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' ;;
    esac
}

# This machine's address on the local network, when it can be told for certain: the address
# of the interface the default route uses, if that is an Ethernet or Wi-Fi interface with a
# private address. Otherwise nothing (a VPN, a virtual machine's own network, no network).
lan_address() {
    found=""
    case $(uname -s) in
        Darwin)
            interface=$(route -n get default 2>/dev/null | sed -n 's/^[[:space:]]*interface:[[:space:]]*//p')
            case $interface in
                en[0-9]*) found=$(ipconfig getifaddr "$interface" 2>/dev/null) || found="" ;;
            esac
            ;;
        Linux)
            if ! grep -qi microsoft /proc/version 2>/dev/null; then
                way=$(ip -4 route get 1.1.1.1 2>/dev/null) || way=""
                interface=$(printf '%s\n' "$way" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
                case $interface in
                    e* | w*) found=$(printf '%s\n' "$way" | sed -n 's/.* src \([0-9.]*\).*/\1/p') ;;
                esac
            fi
            ;;
    esac
    case $found in
        10.*.*.* | 192.168.*.* | 172.1[6-9].*.* | 172.2[0-9].*.* | 172.3[01].*.*) printf '%s' "$found" ;;
    esac
}

# A new file is written under another name ($tmp_file, removed when the run stops) and
# then given its own: whole or not at all, and never over a file that is there.
begin_new() {
    tmp_file=$1.new.$$
    rm -f "$tmp_file"
}

commit_new() {
    if [ -e "$1" ] || [ -L "$1" ]; then
        fail "$1 appeared while the setup ran: it is kept as it is"
    fi
    if ! ln "$tmp_file" "$1" 2>/dev/null; then
        # A file system without hard links, or the file appeared this instant: written
        # without overwriting (the shell refuses an existing file).
        (set -C && cat "$tmp_file" >"$1") 2>/dev/null ||
            fail "$1 could not be written (if it appeared while the setup ran, it is kept as it is)"
    fi
    rm -f "$tmp_file"
    tmp_file=""
}

ask() {
    # ask <question> [default]: the answer in $answer.
    if [ -n "${2:-}" ]; then
        printf '%s [%s]: ' "$1" "$2"
    else
        printf '%s: ' "$1"
    fi
    IFS= read -r answer || fail "no answer (the input ended)"
    [ -n "$answer" ] || answer=${2:-}
}

ask_secret() {
    # ask_secret <question>: the answer in $answer, typed without echo (switched off before
    # the question shows, so that nothing typed early is echoed either).
    tty_state=$(stty -g)
    stty -echo
    printf '%s: ' "$1"
    IFS= read -r answer || fail "no answer (the input ended)"
    stty "$tty_state"
    tty_state=""
    printf '\n'
}

# --- checks that need no answer ----------------------------------------------------------

if [ ! -f "$packaging/compose.yaml" ] || [ ! -f "$packaging/shijhon.example.toml" ]; then
    fail "packaging/compose.yaml is not next to this script: run it from a checkout of Shijhon"
fi
# Passwords go through printf into pipes: as a builtin it never shows in the process list.
[ "$(command -v printf)" = printf ] || fail "this shell's printf is not a builtin: run the script with sh, dash or bash"
command -v docker >/dev/null 2>&1 ||
    fail "Docker is not installed. Install Docker with Compose (https://docs.docker.com/get-docker/), then run ./setup.sh again."
docker compose version >/dev/null 2>&1 ||
    fail "Docker is there, Compose is not ('docker compose version' fails). Install the Compose plugin, then run ./setup.sh again."
docker_system=$(docker info --format '{{.OperatingSystem}}' 2>/dev/null) ||
    fail "Docker is not running, or this user may not use it ('docker info' fails). Start it, then run ./setup.sh again."

# What Compose finds in the shell's environment it takes in place of packaging/.env, in
# this run and in every later command: the script and Compose would then differ.
set_here=""
for name in MUSIC_DIR NAVIDROME_DATA SHIJHON_DATA USAGE_EXPORT_DATA PUID PGID SHIJHON_PORT \
    SHIJHON_BIND SHIJHON_ADAPTERS; do
    eval "set_here=\${$name+set}"
    [ -z "$set_here" ] ||
        fail "$name is set in this shell: Docker Compose would use it in place of packaging/.env. Run 'unset $name', then ./setup.sh again."
done

# Another installation under the same Compose project name would be taken over by this one.
project=${COMPOSE_PROJECT_NAME:-}
if [ -z "$project" ] && [ -f "$env_file" ]; then
    project=$(env_value COMPOSE_PROJECT_NAME) ||
        fail "COMPOSE_PROJECT_NAME in packaging/.env cannot be read: write it as COMPOSE_PROJECT_NAME=name"
fi
[ -n "$project" ] || project=$(sed -n 's/^name:[[:space:]]*//p' "$packaging/compose.yaml")
others=$(docker ps -a --filter "label=com.docker.compose.project=$project" \
    --format '{{.Label "com.docker.compose.project.working_dir"}}') ||
    fail "the containers on this machine could not be listed ('docker ps' fails)"
printf '%s\n' "$others" | sort -u |
    while IFS= read -r other; do
        [ -n "$other" ] || continue
        [ "$(resolved "$other" 2>/dev/null || printf '%s' "$other")" != "$packaging" ] || continue
        printf 'setup: %s\n' "the Compose project \"$project\" already has containers, from $other. This setup would take them over. Use that installation, or remove it first (docker compose down, in that folder)." >&2
        exit 1
    done || exit 1

# --- where the data is kept --------------------------------------------------------------

# Compose keeps Navidrome's data, Shijhon's and the usage export each in a Docker volume of
# this project, unless packaging/.env names a folder in its place. Set from packaging/.env:
# each a folder's absolute path, or empty for the volume.
navidrome_data=""
shijhon_data=""
usage_data=""

# The folder packaging/.env names for one of them, or nothing for its volume (the value left
# out, or the volume's own name). Compose takes a value that starts with /, . or ~ for a
# folder (a relative one from packaging/), and any other for a volume's name: only the
# three it declares are there.
data_folder() {
    value=$(env_value "$1") || return 1
    case $value in
        "" | "$2") ;;
        /*) printf '%s' "$value" ;;
        .*) printf '%s/%s' "$packaging" "$value" ;;
        \~ | \~/*) home_expanded "$value" ;;
        *) return 1 ;;
    esac
}

# project_volumes [volume]: which of this project's data volumes (or that one alone) exist,
# by the names Compose gives them - also one made otherwise, from a backup say.
project_volumes() {
    all=$(docker volume ls -q) ||
        fail "the volumes on this machine could not be listed ('docker volume ls' fails)"
    for volume in ${1:-navidrome-data shijhon-data usage-export}; do
        printf '%s\n' "$all" | grep -x -F -e "${project}_$volume" || :
    done
}

navidrome_where() {
    if [ -n "$navidrome_data" ]; then
        printf '%s' "$navidrome_data"
    else
        printf 'the Docker volume %s_navidrome-data' "$project"
    fi
}

# --- what is there already ---------------------------------------------------------------

have_env=false
bind=""
if [ -f "$env_file" ]; then
    have_env=true
    if [ -n "$music$port" ] || $adapters_given; then
        fail "packaging/.env exists and is used as it is. Change it there, or run again without --music, --port and --adapters."
    fi
    if ! { music=$(env_value MUSIC_DIR) && port=$(env_value SHIJHON_PORT) &&
        bind=$(env_value SHIJHON_BIND) && [ -n "$music" ]; }; then
        fail "packaging/.env exists, but MUSIC_DIR, SHIJHON_PORT or SHIJHON_BIND in it cannot be read. Write each as NAME='value', or move the file away to start afresh."
    fi
    [ -n "$port" ] || port=$default_port
    # A relative path there is Compose's: from packaging/.
    case $music in /*) ;; *) music=$packaging/$music ;; esac
    [ -d "$music" ] || fail "packaging/.env names the music folder $music, which is not there"
    say "packaging/.env exists: used as it is (music in $music, port $port)."
    if ! { navidrome_data=$(data_folder NAVIDROME_DATA navidrome-data) &&
        shijhon_data=$(data_folder SHIJHON_DATA shijhon-data) &&
        usage_data=$(data_folder USAGE_EXPORT_DATA usage-export); }; then
        fail "NAVIDROME_DATA, SHIJHON_DATA or USAGE_EXPORT_DATA in packaging/.env is not a folder's path, or cannot be read. Give each as NAME='/path/to/folder', or leave it out for the Docker volume."
    fi
    if [ -n "$navidrome_data$shijhon_data$usage_data" ] && [ "$docker_system" = "Docker Desktop" ]; then
        say "Warning: packaging/.env keeps data in a folder of this computer. On Docker Desktop that can corrupt the databases: keep them in Docker volumes (docs/known-issues.md)."
    fi
else
    # This project's volumes without packaging/.env: an earlier installation's data, which
    # this setup would take over.
    volumes=$(project_volumes)
    if [ -n "$volumes" ]; then
        volumes=$(printf '%s\n' "$volumes" | tr '\n' ' ')
        fail "Docker has volumes of the Compose project \"$project\", an earlier installation's data: ${volumes% }. To go on with that installation, put its packaging/.env and packaging/config back. To start afresh, remove them first, which deletes that data: docker volume rm ${volumes% }"
    fi
fi

# A configuration that believes a reverse proxy, with the port open to every address: a
# device that connects directly from a trusted address would be believed too. The .env has
# to say which is meant.
if [ -z "$bind" ] && [ -f "$config_file" ] &&
    grep -q '^[[:space:]]*trusted_proxies[[:space:]]*=' "$config_file"; then
    fail "packaging/config/shijhon.toml names trusted proxies, and packaging/.env does not say where Shijhon's port is published. For a reverse proxy on this machine, add SHIJHON_BIND=127.0.0.1 to packaging/.env; to publish the port on every address all the same (the proxy is on another machine), SHIJHON_BIND=0.0.0.0. Then run ./setup.sh again."
fi

# --- the questions -----------------------------------------------------------------------

music_problem() {
    if [ ! -d "$1" ]; then
        say "there is no folder $1"
    elif [ ! -r "$1" ] || [ ! -x "$1" ]; then
        say "the folder $1 cannot be read by this user"
    elif [ -d "$1/_shijhon" ] && [ ! -w "$1/_shijhon" ]; then
        say "the folder $1/_shijhon cannot be written by this user"
    elif [ ! -d "$1/_shijhon" ] && [ ! -w "$1" ]; then
        say "Shijhon needs a folder _shijhon inside $1 that this user can write: create it, or allow this user to"
    else
        path_problem "$(resolved "$1")"
    fi
}

port_problem() {
    case $1 in
        "" | 0* | *[!0-9]*)
            say "the port is a number from 1 to 65535"
            return 0
            ;;
    esac
    if [ "${#1}" -gt 5 ] || [ "$1" -gt 65535 ]; then
        say "the port is a number from 1 to 65535"
    elif port_in_use "$1"; then
        say "port $1 is in use on this machine"
    else
        return 1
    fi
}

# Whether something listens on the port, on loopback or on the machine's network address,
# as far as that can be told without Docker (nc is there on most systems). Docker's own
# answer comes when Shijhon starts.
port_in_use() {
    command -v nc >/dev/null 2>&1 || return 1
    if nc -z -w 2 127.0.0.1 "$1" >/dev/null 2>&1; then
        return 0
    fi
    address=$(lan_address) || address=""
    [ -n "$address" ] && nc -z -w 2 "$address" "$1" >/dev/null 2>&1
}

user_problem() {
    if [ -z "$1" ]; then
        say "the username is empty"
    elif [ "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" = "$service_user" ]; then
        say "\"$service_user\" is the name of Shijhon's own account: choose another"
    elif has_control "$1"; then
        say "the username has a character that cannot be typed"
    else
        return 1
    fi
}

password_problem() {
    if [ -z "$1" ]; then
        say "the password is empty"
    elif has_control "$1"; then
        say "the password has a character that cannot be typed (a tab, an escape)"
    else
        return 1
    fi
}

read_password_file() {
    if [ "$own_password_file" = "-" ]; then
        IFS= read -r own_password || [ -n "$own_password" ] || fail "no password on the standard input"
    else
        [ -r "$own_password_file" ] || fail "the password file $own_password_file cannot be read"
        IFS= read -r own_password <"$own_password_file" || [ -n "$own_password" ] ||
            fail "the password file $own_password_file is empty"
    fi
    own_password=${own_password%"$(printf '\r')"}
    problem=$(password_problem "$own_password") && fail "$problem"
    return 0
}

# Your own account's name and password: from the options, or asked.
own_account() {
    if [ -n "$own_user" ]; then
        problem=$(user_problem "$own_user") && fail "$problem"
    elif $questions; then
        while :; do
            ask "A username for your own account"
            own_user=$answer
            problem=$(user_problem "$own_user") || break
            say "  $problem."
        done
    else
        fail "your own account needs a name and a password: --user NAME --password-file FILE"
    fi
    if [ -n "$own_password_file" ]; then
        read_password_file
    elif $questions; then
        while :; do
            ask_secret "A password for it (not shown)"
            own_password=$answer
            if problem=$(password_problem "$own_password"); then
                say "  $problem."
                continue
            fi
            ask_secret "The password again"
            [ "$answer" != "$own_password" ] || break
            say "  The two differ."
        done
        answer=""
    else
        fail "your own account needs a password: --password-file FILE (\"-\": standard input)"
    fi
}

# A Navidrome database is this setup's own only when packaging/.env and Shijhon's password
# file are there with it (the password is written before Navidrome first starts; without
# packaging/.env this project's volumes stopped the run above): any other stops the run,
# before it is started or touched. Without a database the accounts are made in this run:
# yours is asked for now, before any work. In a volume the database is taken to be there
# once the volume is.
accounts_first() {
    if [ -n "$navidrome_data" ]; then
        database=""
        [ ! -f "$navidrome_data/navidrome.db" ] || database=there
    else
        database=$(project_volumes navidrome-data)
    fi
    if [ -z "$database" ]; then
        own_account
    elif [ ! -f "$password_file" ]; then
        fail "Navidrome's data is there already ($(navidrome_where)), and there is no packaging/config/navidrome-password. If it is this installation's, put the password of its admin account \"$service_user\" in that file (mode 0600) and run ./setup.sh again; for a Navidrome you already run, see docs/deployment.md, \"Starting from an existing Navidrome\"."
    fi
}

if [ -n "$own_user" ]; then
    problem=$(user_problem "$own_user") && fail "$problem"
fi

if $have_env; then
    accounts_first
else
    if [ -n "$music" ]; then
        problem=$(music_problem "$music") && fail "$problem"
    elif $questions; then
        while :; do
            ask "Where is your music? (a folder)"
            music=$(home_expanded "$answer")
            [ -n "$music" ] || continue
            problem=$(music_problem "$music") || break
            say "  $problem."
        done
    else
        fail "where is your music? --music DIR (./setup.sh --help)"
    fi
    music=$(resolved "$music")
    accounts_first

    if [ -n "$port" ]; then
        problem=$(port_problem "$port") && fail "$problem"
    else
        port=$default_port
        if $questions; then
            while :; do
                ask "The port your apps connect to" "$port"
                port=$answer
                problem=$(port_problem "$port") || break
                say "  $problem."
                port=$default_port
            done
        else
            problem=$(port_problem "$port") && fail "$problem: choose another with --port"
        fi
    fi

    if ! $adapters_given && $questions; then
        ask "Catalog adapters to install, if any (docs/catalogs.md)" ""
        adapters=$answer
    fi
    case $adapters in
        *"'"* | *"$newline"*) fail "the catalog adapters cannot contain a quote or a line break" ;;
    esac
fi

# --- folders and files -------------------------------------------------------------------

# The folders with the permissions this user's folders usually get (other programs may
# read the music folder); everything else this script writes is private. A data folder
# that packaging/.env names is made here too, so that it is this user's.
(
    umask "$folder_umask" || exit 1
    mkdir -p "$music/_shijhon" || exit 1
    for folder in "$navidrome_data" "$shijhon_data" "$usage_data"; do
        if [ -n "$folder" ]; then
            mkdir -p "$folder" || exit 1
        fi
    done
) || fail "the folder $music/_shijhon, or a data folder that packaging/.env names, could not be created"

if ! $have_env; then
    begin_new "$env_file"
    {
        env_line MUSIC_DIR "$music"
        printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)"
        [ "$port" = "$default_port" ] || printf 'SHIJHON_PORT=%s\n' "$port"
        [ -z "$adapters" ] || env_line SHIJHON_ADAPTERS "$adapters"
    } >"$tmp_file"
    commit_new "$env_file"
    did "packaging/.env written, the folder $music/_shijhon created"
fi

mkdir -p "$config_dir"
if [ -f "$config_file" ]; then
    say "packaging/config/shijhon.toml exists: kept."
else
    begin_new "$config_file"
    cat "$packaging/shijhon.example.toml" >"$tmp_file"
    commit_new "$config_file"
    did "packaging/config/shijhon.toml written, from the example"
fi

# The password of Shijhon's own Navidrome account, before Navidrome first starts: a
# database is then never there without it.
if [ ! -f "$password_file" ]; then
    service_password=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#service_password}" -eq 48 ] || fail "no random password could be made (/dev/urandom)"
    begin_new "$password_file"
    printf '%s\n' "$service_password" >"$tmp_file"
    commit_new "$password_file"
    service_password=""
    made_password_file=true
    did "a password for Shijhon's own Navidrome account written to packaging/config/navidrome-password"
fi

# --- Navidrome and its accounts ----------------------------------------------------------

# One request to Navidrome, from inside its own container: <path> [<JSON body>] [<token>].
# Everything - the address, the body with its password, the token - is given to curl on
# its standard input. The answer's body in $nd_body, its status in $nd_status.
nd_request() {
    nd_answer=$(
        {
            printf 'url = "http://127.0.0.1:4533%s"\n' "$1"
            if [ -n "${2:-}" ]; then
                printf 'header = "Content-Type: application/json"\n'
                printf 'data = "%s"\n' "$(escaped "$2")"
            fi
            if [ -n "${3:-}" ]; then
                printf 'header = "X-ND-Authorization: Bearer %s"\n' "$3"
            fi
        } | compose exec -T navidrome curl -sS -m 30 -o - -w '\n%{http_code}' -K - 2>/dev/null
    ) || nd_answer="${newline}000"
    nd_status=${nd_answer##*"$newline"}
    nd_body=${nd_answer%"$newline"*}
}

token_of() {
    printf '%s' "$1" | LC_ALL=C sed -n 's/.*"token":"\([^"]*\)".*/\1/p'
}

say "Starting Navidrome ..."
compose up -d navidrome || fail "Navidrome could not be started (the message above says why)"
tries=0
until compose exec -T navidrome curl -fsS -m 10 -o /dev/null http://127.0.0.1:4533/ping </dev/null 2>/dev/null; do
    tries=$((tries + 1))
    [ "$tries" -lt 120 ] || fail "Navidrome does not answer after two minutes: docker compose logs navidrome (in packaging/)"
    sleep 1
done
did "Navidrome started"

token=""
IFS= read -r service_password <"$password_file" || [ -n "$service_password" ] ||
    fail "packaging/config/navidrome-password is empty: put the password of Navidrome's account \"$service_user\" in it"
nd_request /auth/login "{\"username\":\"$service_user\",\"password\":\"$(escaped "$service_password")\"}"
[ "$nd_status" != 200 ] || token=$(token_of "$nd_body")
if [ -z "$token" ]; then
    nd_request /auth/createAdmin "{\"username\":\"$service_user\",\"password\":\"$(escaped "$service_password")\"}"
    case $nd_status in
        200)
            token=$(token_of "$nd_body")
            did "Navidrome's account \"$service_user\" created for Shijhon"
            ;;
        403)
            # Navidrome has accounts. If "shijhon" with this password is one of them now,
            # another run of this script made it meanwhile. Otherwise this is not the
            # setup's own Navidrome, and a password made in this run fits nothing.
            nd_request /auth/login "{\"username\":\"$service_user\",\"password\":\"$(escaped "$service_password")\"}"
            [ "$nd_status" != 200 ] || token=$(token_of "$nd_body")
            if [ -z "$token" ]; then
                ! $made_password_file || rm -f "$password_file"
                fail "Navidrome ($(navidrome_where)) already has accounts, and \"$service_user\" with the password in packaging/config/navidrome-password is not one of them. Create that admin account in Navidrome and put its password in that file (mode 0600), then run ./setup.sh again."
            fi
            ;;
        *) fail "Navidrome answered $nd_status when its first account was created: docker compose logs navidrome (in packaging/)" ;;
    esac
fi
[ -n "$token" ] || fail "Navidrome's answer could not be read"
service_password=""

nd_request /api/user "" "$token"
[ "$nd_status" = 200 ] || fail "Navidrome answered $nd_status when asked for its accounts"
accounts=$(printf '%s' "$nd_body" | LC_ALL=C grep -o '"userName":"[^"]*"' | LC_ALL=C sed -e 's/^"userName":"//' -e 's/"$//')
others=$(printf '%s\n' "$accounts" | grep -v -i -x -F -e "$service_user" || :)

if [ -z "$others" ] && [ -z "$own_user" ]; then
    own_account
fi
if [ -n "$own_user" ]; then
    if printf '%s\n' "$accounts" | grep -q -i -x -F -e "$(escaped "$own_user")"; then
        say "Navidrome has the account \"$own_user\": kept, with the password it has."
    else
        [ -n "$own_password" ] || own_account
        nd_request /api/user "{\"userName\":\"$(escaped "$own_user")\",\"name\":\"$(escaped "$own_user")\",\"password\":\"$(escaped "$own_password")\",\"isAdmin\":true}" "$token"
        case $nd_status:$nd_body in
            200:*) did "your account \"$own_user\" created in Navidrome (an admin)" ;;
            400:*validation.unique*) say "Navidrome has the account \"$own_user\": kept, with the password it has." ;;
            *) fail "Navidrome answered $nd_status when the account \"$own_user\" was created" ;;
        esac
    fi
else
    say "Navidrome has its accounts: kept."
fi
own_password=""
token=""

# --- everything ----------------------------------------------------------------------------

# Built first, started second: containers made by the command that also built their image
# are made again by the next "up", and a second run of this script should change nothing.
say "Building Shijhon's image (the first build takes a few minutes) ..."
compose build || fail "Shijhon's image could not be built (the message above says why)"
say "Starting Shijhon ..."
compose up -d ||
    fail "Shijhon could not be started (the message above says why; when it is the port, change SHIJHON_PORT in packaging/.env)"
tries=0
until compose exec -T shijhon python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/rest/ping.view', timeout=5)" </dev/null 2>/dev/null; do
    tries=$((tries + 1))
    [ "$tries" -lt 120 ] || fail "Shijhon does not answer after two minutes: docker compose logs shijhon (in packaging/)"
    sleep 1
done

say ""
say "Shijhon is running. Connect your apps to it, never to Navidrome:"
say ""
open=false
case $bind in
    "" | 0.0.0.0)
        open=true
        address=$(lan_address) || address=""
        [ -n "$address" ] || address="<this machine's address>"
        say "  on this machine      http://127.0.0.1:$port"
        say "  from your network    http://$address:$port"
        say "  the dashboard        http://127.0.0.1:$port/shijhon/"
        ;;
    127.* | localhost)
        say "  on this machine      http://127.0.0.1:$port"
        say "  the dashboard        http://127.0.0.1:$port/shijhon/"
        ;;
    *)
        say "  your apps            http://$bind:$port"
        say "  the dashboard        http://$bind:$port/shijhon/"
        ;;
esac
say ""
say "Sign in with your Navidrome account${own_user:+ ($own_user)}."
if $open; then
    say "The connection is not encrypted, as with a plain Navidrome: this is for a network"
    say "you trust. For access from outside, see docs/deployment.md (a TLS reverse proxy)."
fi
if [ -z "$navidrome_data$shijhon_data$usage_data" ]; then
    say "The databases are in Docker volumes. Backups: docs/deployment.md, \"Backups\"."
fi
say "Next: a catalog (docs/catalogs.md) and add-ons (docs/add-ons.md)."
