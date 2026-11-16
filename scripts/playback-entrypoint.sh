#!/bin/sh
# Media and transcode must never silently fall back to the guest root filesystem.
set -eu
: "${PLAYBACK_NFS_SOURCE:?expected NFS export is required}"
: "${PLAYBACK_TRANSCODE_ID:?expected transcode filesystem UUID is required}"
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

check_mounts() {
    awk -v expected="$PLAYBACK_NFS_SOURCE" -v identity="$PLAYBACK_TRANSCODE_ID" '
    function has(csv, item) { return index("," csv ",", "," item ",") != 0 }
    BEGIN {
        if (split(identity, uuid, "-") != 5 || length(uuid[1]) != 8 ||
            length(uuid[2]) != 4 || length(uuid[3]) != 4 || length(uuid[4]) != 4 ||
            length(uuid[5]) != 12 || identity ~ /[^[:xdigit:]-]/) bad = 1
    }
    FILENAME == ARGV[3] {
        marker_lines++
        if ($0 != identity) bad = 1
        next
    }
    {
        file = FILENAME == ARGV[1] ? 1 : 2
        target = file == 1 ? "/srv/media-view/media" : "/data/media"
        if (index($5, target "/") == 1) bad = 1
        transcode = file == 1 ? "/srv/transcode" : "/transcode"
        if (index($5, transcode "/") == 1) bad = 1
        if (file == 1 && $5 == "/") root_device = $3
        slot = 0
        if ($5 == target) slot = file
        if ($5 == transcode) slot = file + 2
        if (file == 2 && $5 == "/run/transcode-volume-id") slot = 5
        if (!slot) next
        count[slot]++
        sep = 0
        for (i = 7; i <= NF; i++) if ($i == "-") { sep = i; break }
        if (!sep) { bad = 1; next }
        if (slot <= 2) {
            if ($4 != "/" || !has($6, "ro") || has($6, "rw") ||
                $(sep+1) != "nfs4" || $(sep+2) != expected ||
                !has($(sep+3), "vers=4.2") || !has($(sep+3), "hard") ||
                !has($(sep+3), "proto=tcp")) bad = 1
        } else {
            wanted_root = slot == 3 ? "/" : (slot == 4 ? "/data" : "/.homelab-volume-id")
            wanted_mode = slot == 5 ? "ro" : "rw"
            other_mode = slot == 5 ? "rw" : "ro"
            if ($4 != wanted_root || $(sep+1) != "ext4" || !has($6, wanted_mode) ||
                has($6, other_mode) || !has($(sep+3), "rw") || has($(sep+3), "ro")) bad = 1
            if (slot != 5 && (!has($6, "nodev") || !has($6, "nosuid") ||
                !has($6, "noexec"))) bad = 1
        }
        device[slot] = $3
        id[slot] = $1 ":" $3
    }
    END {
        for (slot = 1; slot <= 5; slot++) if (count[slot] != 1) bad = 1
        if (bad || marker_lines != 1 || device[1] != device[2] ||
            device[3] != device[4] || device[3] != device[5] ||
            device[3] == root_device) exit 1
        print id[1] ":" id[2] ":" id[3] ":" id[4] ":" id[5]
    }' "$1" "$2" "$3"
}

check_storage() {
    mounts=$(check_mounts "$1" "$2" "$3") || return 1
    admission=$(sh "$script_dir/playback-admission.sh" "$4" "$5" "$6") || return 1
    printf '%s:%s\n' "$mounts" "$admission"
}

check_runtime() {
    check_storage /run/host-mountinfo /proc/self/mountinfo /run/transcode-volume-id \
        /run/storage-admission/lease /run/guest-boot-id /proc/uptime
}

if [ "${1:-}" = --check ]; then
    shift
    if [ "$#" = 0 ]; then
        set -- /run/host-mountinfo /proc/self/mountinfo /run/transcode-volume-id \
            /run/storage-admission/lease /run/guest-boot-id /proc/uptime
    fi
    [ "$#" = 6 ] || exit 2
    check_storage "$@" || { echo 'playback: storage or admission refused' >&2; exit 1; }
    exit 0
fi

[ "$#" -gt 0 ] || { echo 'playback: command required' >&2; exit 2; }
initial=$(check_runtime) || {
    echo 'playback: storage or admission refused before startup' >&2; exit 1;
}
# Bounded startup probe; hard NFS can still leave a kernel I/O wait on failure.
timeout -k 2 15 sh -c 'test -d /data/media/movies && test -d /data/media/tv' || {
    echo 'playback: library roots unavailable' >&2; exit 1;
}
# The bounded NFS probe can outlast a lease. Revalidate before spawning Jellyfin.
current=$(check_runtime) || current=invalid
[ "$current" = "$initial" ] || {
    echo 'playback: storage or admission changed during startup' >&2; exit 1;
}

"$@" &
child=$!
stop_child() {
    kill -TERM "$child" 2>/dev/null || true
    # Do not leave an unresponsive application serving after storage disappears.
    (sleep 20; kill -KILL "$child" 2>/dev/null || true) &
    killer=$!
    wait "$child" 2>/dev/null || true
    kill "$killer" 2>/dev/null || true
}
trap 'stop_child; exit 0' TERM INT
while kill -0 "$child" 2>/dev/null; do
    current=$(check_runtime) || current=invalid
    if [ "$current" != "$initial" ]; then
        echo 'playback: storage or admission changed; stopping Jellyfin' >&2
        stop_child
        exit 1
    fi
    sleep 5 &
    wait "$!" || true
done
wait "$child"
