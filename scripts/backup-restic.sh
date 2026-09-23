#!/usr/bin/env bash
set -euo pipefail

# Runs on the server as root. Stopping the stack makes the SQLite and application
# files consistent, and the EXIT trap brings it back even when restic fails.
[[ ${EUID} -eq 0 ]] || { echo 'backup requires root' >&2; exit 1; }
repo=${HOMESERVER_RESTIC_REPOSITORY:-/srv/backup-staging/restic}
password_file=${HOMESERVER_RESTIC_PASSWORD_FILE:-/etc/homeserver/restic-password}
[[ -f "$repo/config" && -r "$password_file" ]] || {
  echo 'restic repository or password is unavailable' >&2
  exit 1
}

exec 9>/run/homeserver-backup-restic.lock
flock -n 9 || { echo 'another backup is running' >&2; exit 1; }

# shellcheck disable=SC1091
source /etc/homeserver/server.env
/opt/homeserver/current/scripts/check-mount.sh /srv/data "${HOMESERVER_MEDIA_UUID:?}"

stack_stopped=false
restart_stack() {
  if [[ "$stack_stopped" == true ]]; then
    systemctl start homeserver-stack.service
  fi
}
trap restart_stack EXIT
stack_stopped=true
systemctl stop homeserver-stack.service

restic -r "$repo" --password-file "$password_file" backup \
  /srv/appdata /etc/homeserver /opt/homeserver/releases \
  --exclude "$password_file" \
  --tag daily

restart_stack
stack_stopped=false
trap - EXIT

restic -r "$repo" --password-file "$password_file" forget --keep-last 3 --prune
restic -r "$repo" --password-file "$password_file" check
