#!/bin/sh
# A hard NFS bind must never silently become a local empty directory.
set -eu
: "${PLAYBACK_NFS_SOURCE:?expected NFS export is required}"

check_mounts() {
    awk -v expected="$PLAYBACK_NFS_SOURCE" '
    function has(csv, item) { return index("," csv ",", "," item ",") != 0 }
    FNR == 1 { file++ }
    {
        target = file == 1 ? "/srv/media-view/media" : "/data/media"
        if (index($5, target "/") == 1) bad = 1
        if ($5 != target) next
        count[file]++
        sep = 0
        for (i = 7; i <= NF; i++) if ($i == "-") { sep = i; break }
        if (!sep || $4 != "/" || !has($6, "ro") || has($6, "rw") ||
            $(sep+1) != "nfs4" || $(sep+2) != expected ||
            !has($(sep+3), "vers=4.2") || !has($(sep+3), "hard") ||
            !has($(sep+3), "proto=tcp")) bad = 1
        device[file] = $3
        id[file] = $1 ":" $3
    }
    END {
        if (bad || count[1] != 1 || count[2] != 1 || device[1] != device[2]) exit 1
        print id[1] ":" id[2]
    }' "$1" "$2"
}

if [ "${1:-}" = --check ]; then
    shift
    if [ "$#" = 0 ]; then set -- /run/host-mountinfo /proc/self/mountinfo; fi
    [ "$#" = 2 ] || exit 2
    check_mounts "$1" "$2" || { echo 'playback: mount refused' >&2; exit 1; }
    exit 0
fi

[ "$#" -gt 0 ] || { echo 'playback: command required' >&2; exit 2; }
initial=$(check_mounts /run/host-mountinfo /proc/self/mountinfo) || {
    echo 'playback: mount refused before startup' >&2; exit 1;
}
# Bounded startup probe; hard NFS can still leave a kernel I/O wait on failure.
timeout -k 2 15 sh -c 'test -d /data/media/movies && test -d /data/media/tv' || {
    echo 'playback: library roots unavailable' >&2; exit 1;
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
    current=$(check_mounts /run/host-mountinfo /proc/self/mountinfo) || current=invalid
    if [ "$current" != "$initial" ]; then
        echo 'playback: mount changed; stopping Jellyfin' >&2
        stop_child
        exit 1
    fi
    sleep 5 &
    wait "$!" || true
done
wait "$child"
