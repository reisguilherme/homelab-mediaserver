#!/usr/bin/env bash
set -euo pipefail

# Runs under the desktop user's WSL account. The server key is restricted to
# read-only rsync of the encrypted repository, and local snapshots are retained.
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
repo="$root/backup/restic"
key="$HOME/.ssh/homeserver_backup_ed25519"
password_file="$HOME/.config/homeserver/restic-password"
host=${HOMESERVER_BACKUP_HOST:-192.168.1.19}
[[ -f "$key" && -f "$password_file" && -f "$repo/config" ]] || {
  echo 'backup key, password, or local repository is unavailable' >&2
  exit 1
}

mkdir -p "$repo"
rsync -a --exclude '/locks/' \
  -e "ssh -i $key -o BatchMode=yes -o StrictHostKeyChecking=yes" \
  "reis@$host:/" "$repo/"
restic -r "$repo" --password-file "$password_file" check
restic -r "$repo" --password-file "$password_file" forget \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune
restic -r "$repo" --password-file "$password_file" check
