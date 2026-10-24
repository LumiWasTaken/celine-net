#!/bin/sh
# shellbox: its home as a disk of a fixed size, on the Docker host (docs/DEPLOY.md
# "Shellbox"), the optional hard limit. Root, from the stack folder; once to set it up,
# and again after raising SHELLBOX_HOME_GB.
#
#   SHELLBOX_HOME     the host folder the shellbox mounts as /home/celine (.env)
#   SHELLBOX_HOME_GB  its size in GB (.env; default 50)
#
# The disk is SHELLBOX_HOME.img (ext4), mounted on SHELLBOX_HOME by /etc/fstab before
# Docker starts. The first run moves what the folder holds into it (the shellbox must
# be stopped for that). A larger SHELLBOX_HOME_GB grows it in place; a smaller one is
# refused: shrinking is by hand, with the box stopped.
set -eu
# cron gives /usr/bin:/bin only; losetup and resize2fs live in /usr/sbin
PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
home=${SHELLBOX_HOME:?set SHELLBOX_HOME in .env}
gb=${SHELLBOX_HOME_GB:-50}
img="$home.img"
want=$((gb * 1073741824))

if [ ! -e "$img" ]; then
    if [ -n "$(docker compose ps -q --status running shellbox 2>/dev/null)" ]; then
        echo "stop the shellbox first: docker compose stop shellbox" >&2
        exit 1
    fi
    truncate -s "$want" "$img"
    mkfs.ext4 -q -m 0 -L shellbox-home "$img"
    tmp=$(mktemp -d)
    mount -o loop "$img" "$tmp"
    # with the folder's owner and mode: the disk's root is its home now
    cp -a "$home/." "$tmp/"
    umount "$tmp"
    rmdir "$tmp"
    find "$home" -mindepth 1 -delete
    # the folder under the disk can never be written: an unmounted disk stops its box
    # (shellboxd refuses a home it cannot write) instead of filling the host's disk
    chattr +i "$home"
    echo "$img $home ext4 loop,nofail,x-systemd.before=docker.service 0 2" >> /etc/fstab
    echo "home is a $gb GB disk now"
fi
if ! mountpoint -q "$home"; then
    chattr +i "$home"
    mount "$home"
fi

have=$(stat -c %s "$img")
if [ "$want" -gt "$have" ]; then
    truncate -s "$want" "$img"
    dev=$(losetup -j "$img" | cut -d: -f1)
    losetup -c "$dev"
    resize2fs "$dev" > /dev/null
    echo "home grown to $gb GB"
elif [ "$want" -lt "$have" ]; then
    echo "SHELLBOX_HOME_GB=$gb is below the home's $((have / 1073741824)) GB:" \
         "shrinking is by hand, with the box stopped" >&2
fi
