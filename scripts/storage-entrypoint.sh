#!/bin/sh
# Docker restart also runs this guard, even when Compose init is not rerun.
set -eu
set -f
target=$1
shift
verified=false
logical_count=0
logical_device=
logical_root=
logical_options=
while IFS= read -r record; do
  # mountinfo fields before '-' have a fixed mountpoint position.
  read -r _mount _parent device root mounted options _rest <<EOF
$record
EOF
  if [ "$mounted" = "$target" ]; then
    case "$record" in
      *" - fuse.mergerfs "*)
        verified=true
        logical_count=$((logical_count + 1))
        logical_device=$device
        logical_root=$root
        logical_options=$options
        ;;
    esac
  fi
done < /proc/self/mountinfo
if [ "$verified" != true ]; then
  printf '%s\n' 'Storage startup refused: logical media is not mounted mergerfs.' >&2
  exit 2
fi

# The singleton binds a verified physical root and a non-secret preparation
# attestation. Check them again on every native restart, including when a FUSE
# facade survives an unplugged backing disk.
if [ "${HOMESERVER_STORAGE_SINGLE_GUARD:-}" = 1 ]; then
  refuse_physical() {
    printf '%s\n' 'Storage startup refused: physical media identity is unavailable.' >&2
    exit 2
  }
  [ "$logical_count" -eq 1 ] || refuse_physical
  case "$target:$logical_root" in /data:/|/data/media:/media) ;; *) refuse_physical ;; esac
  # An intentional read-only consumer bind is safe; the host union and physical
  # filesystem must still be writable and match the prepared identity below.
  case ",$logical_options," in *,rw,*|*,ro,*) ;; *) refuse_physical ;; esac
  union_count=0
  while IFS= read -r record; do
    read -r _mount _parent device root mounted options _rest <<EOF
$record
EOF
    [ "$mounted" = /srv/media-view ] || continue
    union_count=$((union_count + 1))
    read -r filesystem _source super_options <<EOF
${record#* - }
EOF
    if [ "$device" != "$logical_device" ] || [ "$root" != / ] \
      || [ "$filesystem" != fuse.mergerfs ]; then
      refuse_physical
    fi
    case ",$options," in *,rw,*) ;; *) refuse_physical ;; esac
    case ",$super_options," in *,rw,*) ;; *) refuse_physical ;; esac
  done < /run/host-mountinfo
  [ "$union_count" -eq 1 ] || refuse_physical
  identity=/run/storage-native.identity
  if [ ! -f "$identity" ] || [ -L "$identity" ]; then refuse_physical; fi
  IFS=' ' read -r protocol pool uuid extra < "$identity" || refuse_physical
  if [ "$protocol" != homeserver-single-media-v1 ] || [ "$pool" != hdd3 ] \
    || [ -n "$extra" ] || [ -z "$uuid" ] || [ "${#uuid}" -gt 128 ]; then
    refuse_physical
  fi
  case "$uuid" in
    *[!A-Za-z0-9_.-]*) refuse_physical ;;
  esac
  [ "$(wc -l < "$identity")" -eq 1 ] || refuse_physical
  [ -L "/run/host-uuids/$uuid" ] || refuse_physical
  link=$(readlink "/run/host-uuids/$uuid") || refuse_physical
  expected_node=${link##*/}
  [ -n "$expected_node" ] || refuse_physical
  case "$expected_node" in
    *[!A-Za-z0-9_.-]*) refuse_physical ;;
  esac

  host_count=0
  host_device=
  while IFS= read -r record; do
    read -r _mount _parent device root mounted options _rest <<EOF
$record
EOF
    [ "$mounted" = /srv/external3 ] || continue
    host_count=$((host_count + 1))
    read -r filesystem source super_options <<EOF
${record#* - }
EOF
    if [ "$root" != / ] || [ "$filesystem" != ext4 ] \
      || [ "$source" != "/dev/$expected_node" ]; then
      refuse_physical
    fi
    case ",$options," in *,rw,*) ;; *) refuse_physical ;; esac
    case ",$super_options," in *,rw,*) ;; *) refuse_physical ;; esac
    host_device=$device
  done < /run/host-mountinfo
  if [ "$host_count" -ne 1 ] || [ -z "$host_device" ]; then refuse_physical; fi

  physical_count=0
  while IFS= read -r record; do
    read -r _mount _parent device root mounted _options _rest <<EOF
$record
EOF
    [ "$mounted" = /run/storage-physical ] || continue
    physical_count=$((physical_count + 1))
    read -r filesystem source super_options <<EOF
${record#* - }
EOF
    if [ "$device" != "$host_device" ] || [ "$root" != / ] || [ "$filesystem" != ext4 ] \
      || [ "$source" != "/dev/$expected_node" ]; then
      refuse_physical
    fi
    case ",$super_options," in *,rw,*) ;; *) refuse_physical ;; esac
  done < /proc/self/mountinfo
  [ "$physical_count" -eq 1 ] || refuse_physical
fi
exec "$@"
