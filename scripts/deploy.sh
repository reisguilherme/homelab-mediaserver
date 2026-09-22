#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: deploy.sh --release SHA --artifact FILE --config FILE [--manifest FILE]" >&2
}

release=''; artifact=''; config=''; manifest=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) release=${2:?missing release SHA}; shift 2 ;;
    --artifact) artifact=${2:?missing artifact}; shift 2 ;;
    --config) config=${2:?missing config}; shift 2 ;;
    --manifest) manifest=${2:?missing manifest}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ "$release" =~ ^[0-9a-fA-F]{40}$ ]] || { echo 'release must be a 40-character SHA' >&2; exit 2; }
[[ -f "$artifact" && -f "$config" ]] || { echo 'artifact and config are required files' >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${HOMESERVER_ROOT:?HOMESERVER_ROOT is required}"
manifest=${manifest:-${HOMESERVER_RELEASE_MANIFEST:-}}
[[ -n "$manifest" && -f "$manifest" ]] || { echo 'release manifest is required' >&2; exit 1; }

python3 "$(dirname "${BASH_SOURCE[0]}")/validate-release.py" \
  --manifest "$manifest" --artifact "$artifact" --release "${release,,}" || exit 1
[[ "${release,,}" == "$release" ]] || { echo 'release SHA must be lowercase' >&2; exit 2; }

lock="$HOMESERVER_ROOT/.deploy.lock"
mkdir -p "$HOMESERVER_ROOT"
lock_dir="$lock.d"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo 'another deployment is running' >&2
  exit 2
fi
trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT

release_dir="$HOMESERVER_ROOT/releases/$release"
temporary="$HOMESERVER_ROOT/releases/.incoming-$release-$$"
[[ ! -e "$release_dir" ]] || { echo "release already exists: $release" >&2; exit 1; }
mkdir -p "$HOMESERVER_ROOT/releases"
cleanup() {
  if [[ -n "$temporary" && -d "$temporary" ]]; then
    rm -rf -- "$temporary"
  fi
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT

python3 "$(dirname "${BASH_SOURCE[0]}")/validate-release.py" \
  --manifest "$manifest" --artifact "$artifact" --release "${release,,}" \
  --extract-to "$temporary" || exit 1
[[ -f "$temporary/deploy/compose.yaml" ]] || {
  echo 'release artifact is missing deploy/compose.yaml' >&2
  exit 1
}
[[ -f "$temporary/scripts/check-mount.sh" && -f "$temporary/scripts/smoke.sh" ]] || {
  echo 'release artifact is missing required operational scripts' >&2
  exit 1
}
cp "$manifest" "$temporary/release.json"
printf '%s\n' "$release" > "$temporary/COMMIT"
mv "$temporary" "$release_dir"
temporary=''
ln -sfn "$release_dir" "$HOMESERVER_ROOT/current"
printf 'deployed release %s\n' "$release"
