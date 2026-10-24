#!/bin/sh
# shellbox-manager: the shellbox's morning reset (docs/DEPLOY.md "Shellbox").
#
# At SHELLBOX_RESET_AT (HH:MM in TZ, docker-compose.yml) it recreates the shellbox
# service, which throws away everything outside its home. The reset is announced on the
# intercom with the nightly restart (docs/DEPLOY.md "Announcing a restart"). It drives
# docker compose through the Docker socket, on the stack folder (read-only at /stack),
# under the stack's own project name.
#
# Before the reset it keeps a snapshot of its home, one folder per day
# (shellbox-snapshots/<date>, out of its reach): a hardlink copy of the previous
# snapshot (cp -al), then rsync -a --delete from its home, so a file that did not change
# costs nothing. Snapshots are kept; a restore is by hand. The folder is beside its
# home: in the same volume when its home is a named volume (docker-compose.yml), next
# to the host folder when it is one (DEPLOY.md "Shellbox", the home disk, so the
# snapshots are not on it). SHELLBOX_SNAPSHOTS, a host folder, puts them there instead.
set -eu
if [ "${1:-}" != reset ]; then
    at=${SHELLBOX_RESET_AT:?docker-compose.yml sets it}
    echo "$((10#${at#*:})) $((10#${at%:*})) * * * shellbox-manager reset > /proc/1/fd/1 2>&1" > /etc/crontabs/root
    echo "shellbox-manager: the shellbox resets at $at ($(date +%Z))"
    exec crond -f -d 8
fi
project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$(hostname)")
compose() { docker compose -p "$project" --project-directory /stack "$@"; }

snapshot() {
    box=$(compose ps -q shellbox)
    [ -n "$box" ] || return 0
    image=$(docker inspect -f '{{.Config.Image}}' "$box")
    home='{{range .Mounts}}{{if eq .Destination "/home/celine"}}{{.Name}}|{{.Source}}{{end}}{{end}}'
    mount=$(docker inspect -f "$home" "$box")
    name=${mount%%|*} source=${mount#*|}
    snaps=${SHELLBOX_SNAPSHOTS:-}
    if [ -n "$name" ]; then
        # a named volume: its home is its shellbox/ (docker-compose.yml)
        set -- -v "$name:/data"
    elif [ -n "$source" ]; then
        # a host folder (no name): the snapshots go next to it
        set -- -v "$source:/data/shellbox:ro"
        snaps=${snaps:-$(dirname "$source")/shellbox-snapshots}
    else
        echo "no mount on /home/celine"
        return 1
    fi
    [ -z "$snaps" ] || set -- "$@" -v "$snaps:/data/shellbox-snapshots"
    # the shellbox image has rsync
    docker run --rm --network none "$@" --entrypoint sh "$image" -c '
        set -eu
        day=$1 s=/data/shellbox-snapshots
        mkdir -p "$s"
        last=$(cd "$s" && ls -1d [0-9]* 2>/dev/null | tail -n 1)
        if [ -n "$last" ] && [ "$last" != "$day" ]; then cp -al "$s/$last" "$s/$day"; fi
        rsync -a --delete /data/shellbox/ "$s/$day/"' sh "$(date +%F)"
}

if snapshot; then
    echo "$(date '+%F %R') home snapshot kept"
else
    echo "$(date '+%F %R') home snapshot failed, the reset goes on"
fi
compose up -d --force-recreate --no-deps shellbox
echo "$(date '+%F %R') shellbox reset"
