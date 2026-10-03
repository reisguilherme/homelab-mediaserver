#!/bin/sh
# Docker restart also runs this guard, even when Compose init is not rerun.
set -eu
set -f
target=$1
shift
verified=false
while IFS= read -r record; do
  # mountinfo fields before '-' have a fixed mountpoint position.
  read -r _mount _parent _device _root mounted _rest <<EOF
$record
EOF
  if [ "$mounted" = "$target" ]; then
    case "$record" in
      *" - fuse.mergerfs "*) verified=true ;;
    esac
  fi
done < /proc/self/mountinfo
if [ "$verified" != true ]; then
  printf '%s\n' 'Storage startup refused: logical media is not mounted mergerfs.' >&2
  exit 2
fi
exec "$@"
