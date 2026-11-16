#!/bin/sh
# The platform owns the lease; the consumer only accepts or refuses it.
set -eu
if [ "$#" = 0 ]; then
    set -- /run/storage-admission/lease /run/guest-boot-id /proc/uptime
fi
[ "$#" = 3 ] || exit 2
for input in "$@"; do [ -f "$input" ] && [ -r "$input" ] || exit 1; done
LC_ALL=C
export LC_ALL
# Read each file once. Byte parsing rejects NUL, CRLF and an unterminated final
# line without a separate read racing the platforms atomic lease replacement.
{
    printf '@1\n'
    od -An -v -tu1 -- "$1" || exit 1
    printf '@2\n'
    od -An -v -tu1 -- "$2" || exit 1
    printf '@3\n'
    od -An -v -tu1 -- "$3" || exit 1
    printf '@done\n'
} | awk '
    function valid_uuid(value, parts) {
        return split(value, parts, "-") == 5 && length(parts[1]) == 8 &&
            length(parts[2]) == 4 && length(parts[3]) == 4 && length(parts[4]) == 4 &&
            length(parts[5]) == 12 && value !~ /[^0-9a-f-]/
    }
    /^@/ {
        if ($0 == "@done") complete = 1
        else section = substr($0, 2) + 0
        next
    }
    {
        for (i = 1; i <= NF; i++) {
            byte = $i + 0
            size[section]++
            if (size[section] > 512 || byte > 126 || (byte < 32 && byte != 10)) {
                bad = 1
                exit 1
            }
            if (byte == 10) {
                count[section]++
                line[section, count[section]] = pending[section]
                pending[section] = ""
            } else pending[section] = pending[section] sprintf("%c", byte)
        }
    }
    END {
        if (bad || !complete || count[1] != 5 || count[2] != 1 || count[3] != 1 ||
            pending[1] != "" || pending[2] != "" || pending[3] != "") exit 1
        if (line[1,1] != "version=1" || line[1,2] != "state=ready" ||
            line[1,3] !~ /^boot_id=/ || line[1,4] !~ /^generation=/ ||
            line[1,5] !~ /^expires_uptime_seconds=/) exit 1
        boot = substr(line[1,3], 9)
        generation = substr(line[1,4], 12)
        expires = substr(line[1,5], 24)
        if (!valid_uuid(boot) || boot != line[2,1] || length(generation) != 32 ||
            generation ~ /[^0-9a-f]/ || expires !~ /^(0|[1-9][0-9]*)$/ ||
            length(expires) > 10 ||
            line[3,1] !~ /^[0-9]+[.][0-9]+ [0-9]+[.][0-9]+$/) exit 1
        split(line[3,1], clock, " ")
        remaining = (expires + 0) - (clock[1] + 0)
        if (remaining <= 0 || remaining > 45) exit 1
        print boot ":" generation
    }
'
