#!/bin/sh
# Sets up Shijhon with Docker Compose, in front of a Navidrome of its own or one you already
# run: the steps of docs/quick-start.md ("By hand") and docs/deployment.md ("In front of a
# Navidrome you already run"), done for you. From a checkout:
#
#   ./setup.sh
#
# It asks which Navidrome, where your music is, and an account: a username and a password
# for your own in a new Navidrome, or an admin account of yours, which creates Shijhon's
# own. ./setup.sh --help lists the options that answer instead. The databases and caches
# are kept in Docker volumes.
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
existing_overlay=compose.existing-navidrome.yaml
default_network=172.30.99

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

Sets up Shijhon with Docker Compose (docs/quick-start.md), in front of a new Navidrome or
one you already run, with the databases and caches in Docker volumes. It asks for what it
needs. Each answer can be given as an option instead:

  --music DIR            your music folder (for a Navidrome you run, the one it scans)
  --user NAME            the username of your own account: created in a new Navidrome;
                         in one you run, an admin account it has, which creates Shijhon's
  --password-file FILE   a file whose first line is that account's password
                         ("-": standard input)
  --navidrome-url URL    put Shijhon in front of the Navidrome you run at this address,
                         as Shijhon's container reaches it (such as http://navidrome:4533)
  --navidrome-data DATA  that Navidrome's data, for the usage export: its folder, or its
                         Docker volume's name (optional; docs/deployment.md)
  --port N               the port your apps connect to (default: $default_port)
  --adapters "SPEC ..."  catalog adapters to install (docs/catalogs.md)
  --agent-url URL        the address AI agents connect to over MCP: an HTTPS origin
                         such as https://music.example.com (optional; an http://
                         address on a home network turns on plain HTTP for it)
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
own_created=false
# A password file written for an account in the Navidrome you run that is not made yet:
# removed however the run stops, so that the next run asks for your admin account again.
account_pending=false

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
    if $account_pending; then
        rm -f "$password_file"
        account_pending=false
    fi
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
agent_url=""
agent_private=false
questions=true
# Whether Shijhon goes in front of a Navidrome that runs already ("" until known), its
# address, and its data for the usage export: a folder or a Docker volume.
existing=""
nd_url=""
nd_data=""
nd_data_given=false
nd_volume=""

need_value() {
    [ "$#" -ge 2 ] || fail "$1 needs a value (./setup.sh --help)"
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --music) need_value "$@"; music=$2; shift 2 ;;
        --user) need_value "$@"; own_user=$2; shift 2 ;;
        --password-file) need_value "$@"; own_password_file=$2; shift 2 ;;
        --navidrome-url) need_value "$@"; nd_url=$2; existing=true; shift 2 ;;
        --navidrome-data) need_value "$@"; nd_data=$2; nd_data_given=true; shift 2 ;;
        --port) need_value "$@"; port=$2; shift 2 ;;
        --adapters) need_value "$@"; adapters=$2; adapters_given=true; shift 2 ;;
        --agent-url) need_value "$@"; agent_url=$2; shift 2 ;;
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

# Let Compose interpret an explicitly configured file set, including quoted values.
env_has() {
    [ -f "$env_file" ] && LC_ALL=C grep -q \
        "^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*[=:]" "$env_file"
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
for name in MUSIC_DIR NAVIDROME_DATA EXISTING_NAVIDROME_DATA EXISTING_NAVIDROME_VOLUME SHIJHON_DATA \
    USAGE_EXPORT_DATA PUID PGID SHIJHON_PORT SHIJHON_BIND SHIJHON_ADAPTERS SHIJHON_NETWORK \
    COMPOSE_PROFILES; do
    eval "set_here=\${$name+set}"
    [ -z "$set_here" ] ||
        fail "$name is set in this shell: Docker Compose would use it in place of packaging/.env. Run 'unset $name', then ./setup.sh again."
done

# Another installation under the same Compose project name would be taken over by this one.
# A name given in this shell only is saved in packaging/.env, so that every later Compose
# command in packaging/ - in another terminal too - acts on this installation.
shell_project=${COMPOSE_PROJECT_NAME:-}
env_project=""
if [ -f "$env_file" ]; then
    env_project=$(env_value COMPOSE_PROJECT_NAME) ||
        fail "COMPOSE_PROJECT_NAME in packaging/.env cannot be read: write it as COMPOSE_PROJECT_NAME=name"
fi
if [ -n "$shell_project" ] && [ -n "$env_project" ] && [ "$shell_project" != "$env_project" ]; then
    fail "COMPOSE_PROJECT_NAME is $shell_project in this shell and $env_project in packaging/.env, and Docker Compose would use the shell's. Run 'unset COMPOSE_PROJECT_NAME' or change packaging/.env, then ./setup.sh again."
fi
default_project=$(sed -n 's/^name:[[:space:]]*//p' "$packaging/compose.yaml")
project=${shell_project:-${env_project:-$default_project}}
save_project=false
if [ -n "$shell_project" ] && [ -z "$env_project" ] && [ "$shell_project" != "$default_project" ]; then
    case $shell_project in
        *[!a-z0-9_-]* | [!a-z0-9]*) fail "COMPOSE_PROJECT_NAME is $shell_project: Compose takes lower-case letters, digits, \"-\" and \"_\", starting with a letter or digit" ;;
    esac
    save_project=true
fi
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

# Keep the running project's overlays when an earlier start used -f rather than
# COMPOSE_FILE. An explicit COMPOSE_FILE in the shell or .env takes precedence.
remember_compose_files=false
compose_files=""
if [ "${COMPOSE_FILE+set}" != set ] && ! env_has COMPOSE_FILE && [ -n "$others" ]; then
    compose_files=$(docker ps -a --filter "label=com.docker.compose.project=$project" \
        --format '{{.Label "com.docker.compose.project.config_files"}}') ||
        fail "the project's Compose files could not be read ('docker ps' fails)"
    # Unchanged containers can still name an earlier prefix of the file set.
    # Keep the longest set only when it includes every other set in that order.
    compose_files=$(printf '%s\n' "$compose_files" | awk '
        NF { sets[$0] = 1; if (length($0) > length(longest)) longest = $0 }
        END {
            for (files in sets)
                if (files != longest && index(longest, files ",") != 1) exit 1
            print longest
        }
    ') || fail "the project's containers use conflicting Compose file sets. Set COMPOSE_FILE in packaging/.env to the files to use, then run ./setup.sh again."
    if [ -n "$compose_files" ]; then
        separator=${COMPOSE_PATH_SEPARATOR:-}
        if [ -z "$separator" ] && [ -f "$env_file" ]; then
            separator=$(env_value COMPOSE_PATH_SEPARATOR) ||
                fail "COMPOSE_PATH_SEPARATOR in packaging/.env cannot be read"
        fi
        compose_files=$(printf '%s' "$compose_files" | tr ',' "${separator:-:}")
        COMPOSE_FILE=$compose_files
        export COMPOSE_FILE
        remember_compose_files=true
    fi
fi

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

# A packaging/.env without MUSIC_DIR holds no answers yet: the setup asks them and adds them
# to it. Until then it may hold only the project's name (and Compose's path separator).
have_env=false
partial_env=false
bind=""
if [ -f "$env_file" ] && ! env_has MUSIC_DIR; then
    if LC_ALL=C grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' \
        -e '^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}COMPOSE_PROJECT_NAME[[:space:]]*[=:]' \
        -e '^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}COMPOSE_PATH_SEPARATOR[[:space:]]*[=:]' \
        "$env_file" >/dev/null; then
        fail "packaging/.env has no MUSIC_DIR, so the setup has not written it yet, and it has other lines than COMPOSE_PROJECT_NAME. Move the file away, or write it whole as docs/quick-start.md (\"By hand\") shows, then run ./setup.sh again."
    fi
    partial_env=true
fi
if [ -f "$env_file" ] && ! $partial_env; then
    have_env=true
    if [ -n "$music$port" ] || $adapters_given; then
        fail "packaging/.env exists and is used as it is. Change it there, or run again without --music, --port and --adapters."
    fi
    if ! { music=$(env_value MUSIC_DIR) && port=$(env_value SHIJHON_PORT) &&
        bind=$(env_value SHIJHON_BIND); }; then
        fail "packaging/.env exists, but MUSIC_DIR, SHIJHON_PORT or SHIJHON_BIND in it cannot be read. Write each as NAME='value', or move the file away to start afresh."
    fi
    [ -n "$music" ] ||
        fail "packaging/.env has no value for MUSIC_DIR: write your music folder there as MUSIC_DIR='/path/to/music', or move the file away to start afresh."
    [ -n "$port" ] || port=$default_port
    # A relative path there is Compose's: from packaging/.
    case $music in /*) ;; *) music=$packaging/$music ;; esac
    [ -d "$music" ] || fail "packaging/.env names the music folder $music, which is not there"
    say "packaging/.env exists: used as it is (music in $music, port $port)."
    if [ -n "$nd_url" ] || $nd_data_given; then
        fail "packaging/.env exists and is used as it is. Change the Navidrome there and in packaging/config/shijhon.toml, or run again without --navidrome-url and --navidrome-data."
    fi
    files=${COMPOSE_FILE-}
    if [ "${COMPOSE_FILE+set}" != set ]; then
        files=$(env_value COMPOSE_FILE) ||
            fail "COMPOSE_FILE in packaging/.env cannot be read: write it as COMPOSE_FILE='compose.yaml:...'"
    fi
    case $files in
        *"$existing_overlay"*) existing=true ;;
        *) existing=false ;;
    esac
    if ! { navidrome_data=$(data_folder NAVIDROME_DATA navidrome-data) &&
        shijhon_data=$(data_folder SHIJHON_DATA shijhon-data) &&
        usage_data=$(data_folder USAGE_EXPORT_DATA usage-export); }; then
        fail "NAVIDROME_DATA, SHIJHON_DATA or USAGE_EXPORT_DATA in packaging/.env is not a folder's path, or cannot be read. Give each as NAME='/path/to/folder', or leave it out for the Docker volume."
    fi
    if $existing; then
        # The data of the Navidrome you run, read by the usage export alone.
        if ! { nd_data=$(env_value EXISTING_NAVIDROME_DATA) &&
            nd_volume=$(env_value EXISTING_NAVIDROME_VOLUME); }; then
            fail "EXISTING_NAVIDROME_DATA or EXISTING_NAVIDROME_VOLUME in packaging/.env cannot be read: write each as NAME='value'"
        fi
        # NAVIDROME_DATA is the Compose file's own Navidrome's, which does not run then.
        navidrome_data=""
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

# --- Shijhon's network ---------------------------------------------------------------------

# Shijhon's Docker network has a fixed range, a.b.c.0/24 (SHIJHON_NETWORK in packaging/.env
# gives a.b.c), so that Navidrome can trust Shijhon's address, a.b.c.10. A new installation
# gets a range nothing on this machine uses: Docker refuses one that overlaps another of
# its networks, and one that overlaps a network the machine reaches (a LAN, a VPN) would
# hide it from the containers.

# The IPv4 networks in use here, one a line: Docker's own, and the routes of this machine.
used_networks() {
    ids=$(docker network ls -q) || return 1
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086 # an ID a word
        docker network inspect --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' $ids ||
            return 1
    fi
    case $(uname -s) in
        Linux) ip -4 route show table all 2>/dev/null | awk '{ print $1 }' || : ;;
        Darwin) netstat -rn -f inet 2>/dev/null | awk 'found { print $1 } $1 == "Destination" { found = 1 }' || : ;;
    esac
}

# free_network <a.b.c ...>: the first of these ranges (each a.b.c.0/24) that overlaps none
# of the networks on the standard input, or nothing. Routes come as "10.1.0.0/16", or as
# macOS writes them, "10.1/16", "127" (10.1.0.0/16, 127.0.0.0/8); a default route and its
# halves (shorter than /8) are no network on this machine.
free_network() {
    awk -v wanted="$*" '
        function number(text,   part, n, i, value) {
            n = split(text, part, ".")
            value = 0
            for (i = 1; i <= 4; i++) value = value * 256 + (i <= n ? part[i] + 0 : 0)
            return value
        }
        $1 ~ /^[0-9][0-9.]*(\/[0-9]+)?$/ {
            address = $1
            bits = -1
            if (index(address, "/")) {
                bits = substr(address, index(address, "/") + 1) + 0
                address = substr(address, 1, index(address, "/") - 1)
            }
            if (bits < 0) bits = 8 * split(address, part, ".")
            if (bits < 8 || bits > 32) next
            size = 2 ^ (32 - bits)
            start = number(address)
            start -= start % size
            count++
            from[count] = start
            to[count] = start + size - 1
        }
        END {
            n = split(wanted, candidate, " ")
            for (c = 1; c <= n; c++) {
                low = number(candidate[c] ".0")
                high = low + 255
                for (i = 1; i <= count; i++)
                    if (from[i] <= high && to[i] >= low) break
                if (i > count) {
                    print candidate[c]
                    exit
                }
            }
        }'
}

network_problem() {
    case $1 in
        *[!0-9.]* | *..* | .* | *.) return 0 ;;
    esac
    printf '%s\n' "$1" | awk -F. '
        NF != 3 { exit 1 }
        { for (i = 1; i <= 3; i++) if ($i !~ /^(0|[1-9][0-9]?[0-9]?)$/ || $i + 0 > 255) exit 1 }' &&
        return 1
    return 0
}

if $have_env; then
    net=$(env_value SHIJHON_NETWORK) ||
        fail "SHIJHON_NETWORK in packaging/.env cannot be read: write it as SHIJHON_NETWORK=172.30.99"
    [ -n "$net" ] || net=$default_network
    ! network_problem "$net" ||
        fail "SHIJHON_NETWORK in packaging/.env is $net: give the first three numbers of a range, such as 172.30.99"
fi
# The network's name: Compose's own for its project, or, in front of a Navidrome you run,
# one of this installation's that neither project owns, so that your Navidrome can join it.
network_name() {
    if [ "$existing" = true ]; then
        printf '%s-navidrome' "$project"
    else
        printf '%s_default' "$project"
    fi
}
# The ranges of a network, or nothing when it is not there.
network_there() {
    docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$1" 2>/dev/null || :
}
if $have_env; then
    # A range an earlier run gave this installation is its own; any other must be free.
    if [ -z "$(network_there "$(network_name)")" ]; then
        used=$(used_networks) || fail "Docker's networks could not be listed ('docker network ls' fails)"
        if [ -z "$(printf '%s\n' "$used" | free_network "$net")" ]; then
            fail "Shijhon's network range, $net.0/24, overlaps a network on this machine. Set SHIJHON_NETWORK in packaging/.env to another range's first three numbers, one nothing here uses, then run ./setup.sh again."
        fi
    fi
else
    used=$(used_networks) || fail "Docker's networks could not be listed ('docker network ls' fails)"
    candidates=$default_network
    for prefix in 172.30 172.31 10.231; do
        third=100
        while [ "$third" -le 254 ]; do
            candidates="$candidates $prefix.$third"
            third=$((third + 1))
        done
    done
    net=$(printf '%s\n' "$used" | free_network "$candidates")
    [ -n "$net" ] || fail "no free range for Shijhon's Docker network was found. Write SHIJHON_NETWORK=a.b.c into packaging/.env, for a range a.b.c.0/24 nothing on this machine uses, then run ./setup.sh again."
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

# The agent address (Shijhon's agents.public_url): a scheme and a host, with a port if
# needed, and no path. Plain http:// only for this machine, or for a host on a private
# network (Shijhon's agents.private_http). The check follows Shijhon's, and refuses
# rather than guesses: an address Shijhon would rewrite (an IPv4 address with a leading
# zero, a number for a host) or refuse at startup is never written.

# agent_parts <address>: its host in $agent_name (an IPv6 address without its brackets),
# its port in $agent_port ("" when none is given), and whether the two read cleanly in
# $agent_shape (true or false).
agent_parts() {
    agent_rest=${1#*://}
    agent_shape=true
    agent_name=""
    agent_port=""
    case $agent_rest in
        \[*)
            agent_name=${agent_rest#\[}
            agent_name=${agent_name%%\]*}
            case $agent_rest in
                "[$agent_name]") ;;
                "[$agent_name]:"*) agent_port=${agent_rest#"[$agent_name]:"} ;;
                *) agent_shape=false ;;
            esac
            ipv6_address "$agent_name" || agent_shape=false
            ;;
        *:*:*) agent_shape=false ;;
        *:*)
            agent_name=${agent_rest%%:*}
            agent_port=${agent_rest#*:}
            ;;
        *) agent_name=$agent_rest ;;
    esac
    case $agent_rest in
        \[*) ;;
        *)
            case $agent_name in
                "" | *[!A-Za-z0-9.-]*) agent_shape=false ;;
            esac
            # A last label that is a number makes the host an IPv4 address (as browsers
            # read it): only four plain numbers from 0 to 255 are taken.
            agent_last=${agent_name%.}
            agent_last=${agent_last##*.}
            case $agent_last in
                0[xX]*) agent_shape=false ;;
                "" | *[!0-9]*) ;;
                *) ipv4_address "${agent_name%.}" || agent_shape=false ;;
            esac
            ;;
    esac
    case $agent_rest in
        *:) agent_shape=false ;; # a colon without a port
    esac
    case $agent_port in
        "") ;;
        *[!0-9]* | 0*) agent_shape=false ;;
        *)
            if [ "${#agent_port}" -gt 5 ] || [ "$agent_port" -gt 65535 ]; then
                agent_shape=false
            fi
            ;;
    esac
}

ipv4_address() {
    printf '%s\n' "$1" | awk -F. '
        NF != 4 { exit 1 }
        { for (i = 1; i <= 4; i++) if ($i !~ /^(0|[1-9][0-9]?[0-9]?)$/ || $i + 0 > 255) exit 1 }'
}

ipv6_address() {
    case $1 in "" | *[!0-9A-Fa-f:]*) return 1 ;; esac
    printf '%s\n' "$1" | awk '
        function groups(part,   n, found, i) {
            if (part == "") return 0
            n = split(part, found, ":")
            for (i = 1; i <= n; i++)
                if (found[i] !~ /^[0-9A-Fa-f][0-9A-Fa-f]?[0-9A-Fa-f]?[0-9A-Fa-f]?$/) return -1
            return n
        }
        {
            at = index($0, "::")
            if (at == 0) exit (groups($0) == 8 ? 0 : 1)
            left = substr($0, 1, at - 1)
            right = substr($0, at + 2)
            if (index(right, "::")) exit 1
            l = groups(left)
            r = groups(right)
            exit (l >= 0 && r >= 0 && l + r <= 7 ? 0 : 1)
        }'
}

agent_problem() {
    if has_control "$1"; then
        say "the agent address has a character that cannot be typed (a line break, a tab)"
        return 0
    fi
    case $1 in
        https://* | http://*) ;;
        *)
            say "the agent address starts with https://, such as https://music.example.com"
            return 0
            ;;
    esac
    agent_parts "$1"
    if ! $agent_shape; then
        say "the agent address is https:// and a host, with a port if needed, and no path"
    elif agent_plain "$1" && ! private_host "$agent_name"; then
        say "plain http:// is only for a home network: use https:// for this address"
    else
        return 1
    fi
}

# Whether an address is plain http:// other than on this machine.
agent_plain() {
    case $1 in http://*) ;; *) return 1 ;; esac
    agent_parts "$1"
    case $agent_name in localhost | 127.0.0.1 | ::1) return 1 ;; esac
    return 0
}

# Whether a host may be reached over plain HTTP, as Shijhon tells it: any name (your
# network resolves it), and of addresses those of a private network - private and shared
# (100.64.0.0/10) IPv4 addresses, IPv6 unique local addresses, and loopback.
private_host() {
    agent_lower=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    case $agent_lower in
        *:*)
            case $agent_lower in f[cd]??:* | ::1) return 0 ;; esac
            return 1
            ;;
        *[!0-9.]*) return 0 ;;
        10.*.*.* | 192.168.*.* | 172.1[6-9].*.* | 172.2[0-9].*.* | 172.3[01].*.* | 127.*.*.* | \
            100.6[4-9].*.* | 100.[7-9][0-9].*.* | 100.1[01][0-9].*.* | 100.12[0-7].*.*) return 0 ;;
    esac
    return 1
}

# configured <section> <key>: a string setting in the existing configuration file, without a
# "/" at its end, or nothing.
configured() {
    awk -v wanted="[$1]" -v key="$2" '
        /^[[:space:]]*\[/ {
            section = $0
            sub(/#.*/, "", section)
            gsub(/[[:space:]]/, "", section)
            next
        }
        section == wanted && $0 ~ "^[[:space:]]*" key "[[:space:]]*=[[:space:]]*\"" {
            value = $0
            sub(/^[^"]*"/, "", value)
            sub(/".*$/, "", value)
            sub(/\/$/, "", value)
            print value
            exit
        }' "$config_file"
}

# The agent address in an existing configuration file ([agents] public_url), or nothing.
configured_agent_url() {
    configured agents public_url
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

# Your own account's name and password: from the options, or asked. In front of a Navidrome
# you run, an admin account it has, which creates Shijhon's.
own_account() {
    if [ "$existing" = true ]; then
        name_question="An admin account of your Navidrome (Shijhon creates its own account with it)"
        password_question="Its password (not shown)"
        missing="an admin account of your Navidrome is needed, which creates Shijhon's own account"
    else
        name_question="A username for your own account"
        password_question="A password for it (not shown)"
        missing="your own account needs a name and a password"
    fi
    if [ -n "$own_user" ]; then
        problem=$(user_problem "$own_user") && fail "$problem"
    elif $questions; then
        while :; do
            ask "$name_question"
            own_user=$answer
            problem=$(user_problem "$own_user") || break
            say "  $problem."
        done
    else
        fail "$missing: --user NAME --password-file FILE"
    fi
    if [ -n "$own_password_file" ]; then
        read_password_file
    elif $questions; then
        while :; do
            ask_secret "$password_question"
            own_password=$answer
            if problem=$(password_problem "$own_password"); then
                say "  $problem."
                continue
            fi
            # Navidrome tells whether an existing account's password is right.
            [ "$existing" != true ] || break
            ask_secret "The password again"
            [ "$answer" != "$own_password" ] || break
            say "  The two differ."
        done
        answer=""
    else
        fail "$missing: --password-file FILE (\"-\": standard input)"
    fi
}

# A Navidrome database is this setup's own only when packaging/.env and Shijhon's password
# file are there with it (the password is written before Navidrome first starts; without
# packaging/.env this project's volumes stopped the run above): any other stops the run,
# before it is started or touched. Without a database the accounts are made in this run:
# yours is asked for now, before any work. In a volume the database is taken to be there
# once the volume is. In front of a Navidrome you run, Shijhon's account is there once its
# password file is: until then your admin account is asked for.
accounts_first() {
    if [ "$existing" = true ]; then
        [ -f "$password_file" ] || own_account
        return 0
    fi
    if [ -n "$navidrome_data" ]; then
        database=""
        [ ! -f "$navidrome_data/navidrome.db" ] || database=there
    else
        database=$(project_volumes navidrome-data)
    fi
    if [ -z "$database" ]; then
        own_account
    elif [ ! -f "$password_file" ]; then
        fail "Navidrome's data is there already ($(navidrome_where)), and there is no packaging/config/navidrome-password. If it is this installation's, put the password of its admin account \"$service_user\" in that file (mode 0600) and run ./setup.sh again. To put Shijhon in front of a Navidrome you already run, see docs/deployment.md, \"In front of a Navidrome you already run\"."
    fi
}

# Your Navidrome's address, as Shijhon's container reaches it: http(s), a host and a port,
# at the root (no path). Written into the configuration as a TOML string.
nd_url_problem() {
    if has_control "$1"; then
        say "the address has a character that cannot be typed (a line break, a tab)"
        return 0
    fi
    case $1 in
        http://* | https://*) ;;
        *)
            say "the address starts with http:// or https://, such as http://navidrome:4533"
            return 0
            ;;
    esac
    nd_rest=${1#*://}
    case $nd_rest in
        \[*\]) nd_host=$nd_rest nd_port="" ;;
        \[*\]:*)
            nd_host=${nd_rest%%\]*}]
            nd_port=${nd_rest#"$nd_host:"}
            ;;
        *:*) nd_host=${nd_rest%%:*} nd_port=${nd_rest#*:} ;;
        *) nd_host=$nd_rest nd_port="" ;;
    esac
    nd_shape=true
    case $nd_host in
        \[*\]) ipv6_address "$(printf '%s' "$nd_host" | tr -d '[]')" || nd_shape=false ;;
        "" | *[!A-Za-z0-9._-]*) nd_shape=false ;;
    esac
    case $nd_port in
        "") ;;
        *[!0-9]* | 0*) nd_shape=false ;;
        *) if [ "${#nd_port}" -gt 5 ] || [ "$nd_port" -gt 65535 ]; then nd_shape=false; fi ;;
    esac
    if ! $nd_shape; then
        say "the address is http:// or https://, a host and a port, and no path: Navidrome has to be at the root of its address"
        return 0
    fi
    case $nd_host in
        \[*)
            say "Shijhon's Docker network has IPv4 addresses only: give your Navidrome's name or IPv4 address"
            return 0
            ;;
    esac
    case $(printf '%s' "$nd_host" | tr '[:upper:]' '[:lower:]') in
        localhost | 127.*)
            say "in Shijhon's container $nd_host is the container itself: for a Navidrome on this machine, give its container's name (once it joins Shijhon's network) or http://host.docker.internal:<port>"
            return 0
            ;;
    esac
    return 1
}

# nd_data_check <value>: your Navidrome's data, for the usage export, as a folder
# ($nd_data, resolved) or a Docker volume ($nd_volume); otherwise what is wrong, in $problem.
nd_data_check() {
    problem=""
    case $1 in
        /* | \~ | \~/*)
            folder=$(home_expanded "$1")
            if [ ! -d "$folder" ]; then
                problem="there is no folder $folder"
            elif [ ! -r "$folder/navidrome.db" ]; then
                problem="there is no navidrome.db in $folder that this user can read: give the folder Navidrome keeps its database in (its /data)"
            elif ! problem=$(path_problem "$(resolved "$folder")"); then
                problem=""
                nd_data=$(resolved "$folder")
                nd_volume=""
            fi
            ;;
        [!A-Za-z0-9]* | *[!A-Za-z0-9_.-]*)
            problem="give a folder's path (starting with /) or a Docker volume's name"
            ;;
        *)
            if docker volume inspect "$1" >/dev/null 2>&1; then
                nd_volume=$1
                nd_data=""
            else
                problem="Docker has no volume $1 ('docker volume ls' lists them)"
            fi
            ;;
    esac
    [ -z "$problem" ]
}

# The first port from $default_port that nothing listens on (a default, which is asked).
free_port() {
    candidate=$default_port
    while [ "$candidate" -lt $((default_port + 100)) ] && port_in_use "$candidate"; do
        candidate=$((candidate + 1))
    done
    printf '%s' "$candidate"
}

if [ -n "$own_user" ]; then
    problem=$(user_problem "$own_user") && fail "$problem"
fi

if $have_env; then
    accounts_first
else
    if [ -z "$existing" ]; then
        existing=false
        if $questions; then
            say "Shijhon goes in front of Navidrome: a new one that this setup makes, or one that"
            say "you already run (docs/deployment.md, \"In front of a Navidrome you already run\")."
            while :; do
                ask "A new Navidrome, or one that is already running? (new/running)" "new"
                case $answer in
                    new | New | NEW) break ;;
                    running | Running | RUNNING)
                        existing=true
                        break
                        ;;
                esac
                say "  Answer new or running."
            done
        fi
    fi
    if [ -n "$music" ]; then
        problem=$(music_problem "$music") && fail "$problem"
    elif $questions; then
        music_question="Where is your music? (a folder)"
        ! $existing || music_question="Where is your music? (the folder your Navidrome scans)"
        while :; do
            ask "$music_question"
            music=$(home_expanded "$answer")
            [ -n "$music" ] || continue
            problem=$(music_problem "$music") || break
            say "  $problem."
        done
    else
        fail "where is your music? --music DIR (./setup.sh --help)"
    fi
    music=$(resolved "$music")

    if $existing; then
        if [ -n "$nd_url" ]; then
            nd_url=${nd_url%/}
            problem=$(nd_url_problem "$nd_url") && fail "$problem"
        elif $questions; then
            say "Shijhon reaches your Navidrome from its own Docker network. A Navidrome in a container"
            say "on this machine joins that network and is reached by its name; one on this machine"
            say "outside Docker at http://host.docker.internal:<port>; one elsewhere at its address."
            while :; do
                ask "Your Navidrome's address (such as http://navidrome:4533)"
                nd_url=${answer%/}
                problem=$(nd_url_problem "$nd_url") || break
                say "  $problem."
            done
        else
            fail "your Navidrome's address: --navidrome-url URL (./setup.sh --help)"
        fi
    fi
    accounts_first
    if $existing; then
        if $nd_data_given; then
            [ -z "$nd_data" ] || nd_data_check "$nd_data" || fail "$problem"
        elif $questions; then
            say "The usage export reads your Navidrome's database read-only, so that the cleanup"
            say "can tell which of the releases Shijhon added anyone used. Without it the cleanup is off."
            while :; do
                ask "Your Navidrome's data folder, or its Docker volume (blank: none)" ""
                [ -n "$answer" ] || break
                nd_data_check "$answer" && break
                say "  $problem."
            done
        fi
    elif $nd_data_given; then
        fail "--navidrome-data goes with --navidrome-url, for a Navidrome you already run"
    fi

    if [ -n "$port" ]; then
        problem=$(port_problem "$port") && fail "$problem"
    else
        port=$default_port
        if $questions; then
            port=$(free_port)
            while :; do
                ask "The port your apps connect to" "$port"
                port=$answer
                problem=$(port_problem "$port") || break
                say "  $problem."
                port=$(free_port)
            done
        else
            problem=$(port_problem "$port") && fail "$problem: choose another with --port"
        fi
    fi

    if ! $adapters_given && $questions; then
        say "Catalog adapters are optional (an add-on's catalog needs none). For example, the"
        say "MusicBrainz adapter: https://github.com/Jasshl/shijhon-catalog-musicbrainz/archive/refs/tags/v0.1.0b1.tar.gz"
        ask "Catalog adapters to install, if any (docs/catalogs.md; blank: none)" ""
        adapters=$answer
    fi
    case $adapters in
        *"'"* | *"$newline"*) fail "the catalog adapters cannot contain a quote or a line break" ;;
    esac
fi
[ -n "$existing" ] || existing=false

# In front of a Navidrome you run, a network of this installation's name made earlier (by
# hand, or by a run whose packaging/.env is gone) is used with its range, which your
# Navidrome may already be on.
if $existing && ! $have_env; then
    ranges=$(network_there "$(network_name)")
    case $ranges in
        "") ;;
        *.0/24" ")
            net=${ranges%.0/24 }
            if network_problem "$net"; then
                fail "Docker has a network $(network_name) already, with ${ranges% }: remove it (docker network rm $(network_name)), then run ./setup.sh again."
            fi
            say "Shijhon's network $(network_name) is there already: its range, $net.0/24, is used."
            ;;
        *) fail "Docker has a network $(network_name) already, with ${ranges% } in place of a range a.b.c.0/24: remove it (docker network rm $(network_name)), then run ./setup.sh again." ;;
    esac
fi

# In front of a Navidrome you run, the second Compose file needs Compose 2.24.4 or newer
# (its "!override" and "!reset").
if $existing; then
    compose_version=$(docker compose version --short 2>/dev/null) || compose_version=""
    if ! printf '%s\n' "${compose_version#v}" | awk -F. '
        { major = $1 + 0; minor = $2 + 0; patch = $3 + 0 }
        major > 2 || (major == 2 && (minor > 24 || (minor == 24 && patch >= 4))) { ok = 1 }
        END { exit !ok }'; then
        fail "Docker Compose ${compose_version:-of an unknown version} is too old for Shijhon in front of a Navidrome you run: it needs 2.24.4 or newer ('docker compose version'). Update Docker's Compose plugin, then run ./setup.sh again."
    fi
fi

# The address AI agents connect to over MCP, written into a configuration made in this
# run (an existing one is used as it is). Optional: without it agent access stays off.
# A plain http:// address on a home network also turns on plain HTTP for agents.
plain_words="Over plain HTTP, sign-in passwords and agent tokens travel unencrypted on your network."
if [ -n "$agent_url" ]; then
    agent_url=${agent_url%/}
    problem=$(agent_problem "$agent_url") && fail "$problem"
    if [ -f "$config_file" ]; then
        configured=$(configured_agent_url) || configured=""
        if [ "$configured" != "$agent_url" ]; then
            say "packaging/config/shijhon.toml is kept as it is, with ${configured:-no agent address}. To use $agent_url, set it on the dashboard's Agent access page, or as public_url under [agents] in that file."
            agent_url=""
        fi
    elif agent_plain "$agent_url"; then
        agent_private=true
        say "$plain_words"
    fi
elif $questions && [ ! -f "$config_file" ]; then
    while :; do
        ask "The address AI agents connect to, if any (such as https://music.example.com; blank: none)" ""
        agent_url=${answer%/}
        [ -n "$agent_url" ] || break
        if problem=$(agent_problem "$agent_url"); then
            say "  $problem."
            continue
        fi
        agent_plain "$agent_url" || break
        say "  $plain_words"
        ask "  Use it all the same? (yes/no)" "no"
        case $answer in
            y | Y | yes | Yes | YES)
                agent_private=true
                break
                ;;
        esac
        agent_url=""
    done
fi
# Agents reach Shijhon at the port its apps use, unless a proxy in front forwards another.
if [ -n "$agent_url" ] && [ ! -f "$config_file" ]; then
    agent_parts "$agent_url"
    case $agent_url:$agent_port in
        http://*:)
            say "Note: agents will connect to port 80 of $agent_name; Shijhon listens on port $port here, unless a proxy in front forwards it."
            ;;
        *:"$port" | *:) ;;
        *)
            say "Note: Shijhon listens on port $port here, not $agent_port; agents need that port unless a proxy in front forwards it."
            ;;
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
        if $partial_env; then
            # The lines that were there, then the answers.
            cat "$env_file"
            [ -z "$(tail -c 1 "$env_file")" ] || printf '\n'
        fi
        ! $save_project || env_line COMPOSE_PROJECT_NAME "$project"
        env_line MUSIC_DIR "$music"
        printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)"
        [ "$port" = "$default_port" ] || printf 'SHIJHON_PORT=%s\n' "$port"
        [ -z "$adapters" ] || env_line SHIJHON_ADAPTERS "$adapters"
        printf 'SHIJHON_NETWORK=%s\n' "$net"
        if $existing; then
            env_line COMPOSE_FILE "compose.yaml:$existing_overlay"
            [ -z "$nd_data" ] || env_line EXISTING_NAVIDROME_DATA "$nd_data"
            [ -z "$nd_volume" ] || env_line EXISTING_NAVIDROME_VOLUME "$nd_volume"
            [ -z "$nd_data$nd_volume" ] || printf 'COMPOSE_PROFILES=usage-export\n'
        fi
    } >"$tmp_file"
    if $partial_env; then
        mv -f "$tmp_file" "$env_file" || fail "packaging/.env could not be written"
        tmp_file=""
    else
        commit_new "$env_file"
    fi
    did "packaging/.env written, the folder $music/_shijhon created"
elif $save_project; then
    env_line COMPOSE_PROJECT_NAME "$project" >>"$env_file"
fi
if $save_project; then
    say "COMPOSE_PROJECT_NAME=$project is saved in packaging/.env: Compose commands in packaging/ act on this installation, also in another terminal."
fi

if $remember_compose_files; then
    {
        printf '\n'
        env_line COMPOSE_FILE "$compose_files"
    } >>"$env_file"
    did "the running project's Compose files saved in packaging/.env"
fi

mkdir -p "$config_dir"
if [ -f "$config_file" ]; then
    say "packaging/config/shijhon.toml exists: kept."
else
    begin_new "$config_file"
    {
        # The example, with the gateway of this installation's network (for a reverse proxy
        # on this host), and in front of a Navidrome you run its address, without the usage
        # export when there is none.
        LC_ALL=C sed -e "s|172\\.30\\.99\\.1/32|$net.1/32|" "$packaging/shijhon.example.toml" |
            if $existing; then
                LC_ALL=C sed -e "s|^url = \"http://navidrome:4533\".*|url = \"$nd_url\"  # your Navidrome|"
            else
                cat
            fi |
            if $existing && [ -z "$nd_data$nd_volume" ]; then
                LC_ALL=C sed -e 's|^usage_export_path = |# usage_export_path = |'
            else
                cat
            fi
        if [ -n "$agent_url" ]; then
            printf '\n[agents]\npublic_url = "%s"\n' "$agent_url"
            ! $agent_private || printf 'private_http = true\n'
        fi
    } >"$tmp_file"
    commit_new "$config_file"
    did "packaging/config/shijhon.toml written, from the example${agent_url:+, with the agent address}"
fi

# The password of Shijhon's own Navidrome account, before Navidrome first starts: a
# database is then never there without it. (In front of a Navidrome you run, it is written
# when the account is made, below.)
new_service_password() {
    service_password=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#service_password}" -eq 48 ] || fail "no random password could be made (/dev/urandom)"
    begin_new "$password_file"
    printf '%s\n' "$service_password" >"$tmp_file"
    commit_new "$password_file"
    service_password=""
    made_password_file=true
}
if ! $existing && [ ! -f "$password_file" ]; then
    new_service_password
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

if ! $existing; then
    say "Starting Navidrome ..."
    compose up -d navidrome ||
        fail "Navidrome could not be started (the message above says why; when it says \"Pool overlaps\", another network has the range $net.0/24: set SHIJHON_NETWORK in packaging/.env to the first three numbers of a free one)"
    deadline=$(($(date +%s) + 120))
    while :; do
        remaining=$((deadline - $(date +%s)))
        [ "$remaining" -gt 0 ] || fail "Navidrome does not answer after two minutes: docker compose logs navidrome (in packaging/)"
        [ "$remaining" -le 10 ] || remaining=10
        if compose exec -T navidrome curl -fsS -m "$remaining" -o /dev/null http://127.0.0.1:4533/ping </dev/null 2>/dev/null; then
            break
        fi
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
                200:*)
                    did "your account \"$own_user\" created in Navidrome (an admin)"
                    own_created=true
                    ;;
                400:*validation.unique*) say "Navidrome has the account \"$own_user\": kept, with the password it has." ;;
                *) fail "Navidrome answered $nd_status when the account \"$own_user\" was created" ;;
            esac
        fi
    else
        say "Navidrome has its accounts: kept."
    fi
    own_password=""
    token=""
fi

# --- everything ----------------------------------------------------------------------------

# In front of a Navidrome you run: Shijhon's network, which your Navidrome can join.
network_new=false
if $existing; then
    [ -n "$nd_url" ] || nd_url=$(configured navidrome url) || nd_url=""
    network=$(network_name)
    ranges=$(network_there "$network")
    if [ -z "$ranges" ]; then
        # Addresses for other containers from .128 up: .10 stays Shijhon's.
        docker network create --subnet "$net.0/24" --ip-range "$net.128/25" "$network" >/dev/null ||
            fail "Shijhon's Docker network $network could not be made (the message above says why; when it says \"Pool overlaps\", set SHIJHON_NETWORK in packaging/.env to the first three numbers of a free range)"
        did "Shijhon's Docker network $network made ($net.0/24)"
        network_new=true
    else
        case " $ranges" in
            *" $net.0/24 "*) ;;
            *) fail "Docker has a network $network already, with ${ranges% } in place of $net.0/24. Remove it (docker network rm $network), or set SHIJHON_NETWORK in packaging/.env to its range, then run ./setup.sh again." ;;
        esac
    fi
fi

# What your Navidrome changes, with this installation's names.
navidrome_changes() {
    say ""
    say "Now change your Navidrome, so that Shijhon reaches it and it trusts Shijhon's address,"
    say "$net.10. A Navidrome in a container on this machine joins Shijhon's network: in its"
    say "Compose file add these lines, then run \"docker compose up -d\" in its folder."
    say ""
    say "    services:"
    say "      navidrome:"
    say "        environment:"
    say "          ND_EXTAUTH_TRUSTEDSOURCES: $net.10/32"
    say "        networks: [default, shijhon]"
    say "    networks:"
    say "      shijhon:"
    say "        name: $network"
    say "        external: true"
    say ""
    say "A Navidrome elsewhere: docs/deployment.md, \"In front of a Navidrome you already run\","
    say "says which address it trusts and how Shijhon reaches it."
    say ""
}

# Built first, started second: containers made by the command that also built their image
# are made again by the next "up", and a second run of this script should change nothing.
say "Building Shijhon's image (the first build takes a few minutes) ..."
compose build || fail "Shijhon's image could not be built (the message above says why)"

# Shijhon's account in the Navidrome you run, made through your admin account by Shijhon's
# own command, from Shijhon's network: what Shijhon will reach is what is tried. Your
# password goes on the command's standard input; Shijhon's is in its password file. A
# password file this run wrote is removed again when no account was made with it.
existing_account() {
    # A network made a moment ago has no Navidrome on it yet: first the changes, then the try.
    if $network_new; then
        navidrome_changes
        if ! $questions; then
            fail "Shijhon's network $network was just made, so your Navidrome is not on it yet. Make the changes above, then run ./setup.sh again."
        fi
        ask "Press Enter once your Navidrome has joined $network (or type stop)" ""
        case $answer in stop | Stop | STOP) fail "stopped. Run ./setup.sh again once your Navidrome has joined $network." ;; esac
    fi
    new_service_password
    account_pending=true
    while :; do
        say "Creating Shijhon's account in your Navidrome at $nd_url, with \"$own_user\" ..."
        status=0
        printf '%s\n' "$own_password" |
            compose run --rm --no-deps -T shijhon --config /config/shijhon.toml \
                navidrome-accounts --account "$own_user" --account-password-file /dev/stdin \
                --wait 60 --password-file-name packaging/config/navidrome-password \
                --account-setting-name "the admin account" \
                --account-password-setting-name "its password" || status=$?
        case $status in
            0) break ;;
            1)
                say "Shijhon did not get through to your Navidrome at $nd_url (the message above says why)."
                navidrome_changes
                if $questions; then
                    ask "Try again? (yes/no)" "yes"
                    case $answer in y | Y | yes | Yes | YES) continue ;; esac
                fi
                ;;
            2)
                # What is wrong is said above: the account, its password, or its rights - or a
                # "shijhon" account Navidrome has already, which no other account changes.
                if $questions && [ -z "$own_password_file" ]; then
                    ask "Try another admin account, or this one again? (yes/no)" "yes"
                    case $answer in
                        y | Y | yes | Yes | YES)
                            own_user=""
                            own_account
                            continue
                            ;;
                    esac
                fi
                ;;
        esac
        if [ "$status" -eq 2 ]; then
            fail "Shijhon's account could not be made in your Navidrome. If your Navidrome has an account \"$service_user\" already, from an earlier installation, delete it under Users in Navidrome's web interface, or put its password in packaging/config/navidrome-password (mode 0600). Then run ./setup.sh again."
        fi
        if [ "$status" -eq 1 ]; then
            fail "Shijhon's account could not be made in your Navidrome. Run ./setup.sh again once it can be."
        fi
        fail "Shijhon's account could not be made in your Navidrome (the message above says why)"
    done
    account_pending=false
    own_password=""
    did "Shijhon's account \"$service_user\" made in your Navidrome, its password in packaging/config/navidrome-password"
}
if $existing && [ ! -f "$password_file" ]; then
    existing_account
fi
say "Starting Shijhon ..."
compose up -d ||
    fail "Shijhon could not be started (the message above says why; when it is the port, change SHIJHON_PORT in packaging/.env)"
deadline=$(($(date +%s) + 120))
while :; do
    remaining=$((deadline - $(date +%s)))
    [ "$remaining" -gt 0 ] || fail "Shijhon does not answer after two minutes: docker compose logs shijhon (in packaging/)"
    [ "$remaining" -le 5 ] || remaining=5
    if compose exec -T shijhon python -c "import sys, urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/rest/ping.view', timeout=int(sys.argv[1]))" "$remaining" </dev/null 2>/dev/null; then
        break
    fi
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
if [ -n "$agent_url" ]; then
    say "  AI agents (MCP)      $agent_url/shijhon/mcp"
fi
say ""
if $existing; then
    say "Everyone signs in, in apps and on the dashboard, with the Navidrome account they have."
    say "Next, once your apps use Shijhon, remove your Navidrome's published port, so that only"
    say "Shijhon reaches it (docs/deployment.md, \"Moving your apps\")."
    if [ -z "$nd_data$nd_volume" ]; then
        say "Without your Navidrome's data the usage export is off, and with it the cleanup"
        say "(docs/deployment.md, \"The usage export\")."
    fi
elif $own_created; then
    say "Sign in, in apps and on the dashboard, with the account you just created ($own_user)."
elif [ -n "$own_user" ]; then
    say "Sign in, in apps and on the dashboard, with your account ($own_user)."
else
    say "Sign in, in apps and on the dashboard, with the account you created at the first setup."
fi
if $open; then
    say "The connection is not encrypted, as with a plain Navidrome: this is for a network"
    say "you trust. For access from outside, see docs/deployment.md (a TLS reverse proxy)."
fi
if [ -z "$navidrome_data$shijhon_data$usage_data" ]; then
    say "The databases are in Docker volumes. Backups: docs/deployment.md, \"Backups\"."
fi
say "Next: a catalog (docs/catalogs.md) and add-ons (docs/add-ons.md)."
