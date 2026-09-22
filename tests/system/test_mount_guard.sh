#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
script="$repo_root/scripts/check-mount.sh"
test_root=$(mktemp -d)
fake_bin="$test_root/bin"
mount_path="$test_root/data"
mkdir -p "$fake_bin" "$mount_path"
trap 'rm -rf "$test_root"' EXIT

cat > "$fake_bin/findmnt" <<'FAKE_FINDMNT'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_FINDMNT_MODE:-mounted}" == "not-mounted" ]]; then
  exit 1
fi
if [[ "${FAKE_FINDMNT_MODE:-mounted}" == "wrong-target" ]]; then
  printf '/\t%s\trw\n' "${FAKE_FINDMNT_UUID:-test-uuid}"
else
  printf '%s\t%s\t%s\n' "$FAKE_FINDMNT_TARGET" "${FAKE_FINDMNT_UUID:-test-uuid}" "${FAKE_FINDMNT_OPTIONS:-rw}"
fi
FAKE_FINDMNT
chmod +x "$fake_bin/findmnt"

run_guard() {
  PATH="$fake_bin:$PATH" \
    FAKE_FINDMNT_TARGET="$mount_path" \
    FAKE_FINDMNT_UUID="test-uuid" \
    FAKE_FINDMNT_MODE="${1:-mounted}" \
    FAKE_FINDMNT_OPTIONS="${2:-rw}" \
    bash "$script" "$mount_path" "test-uuid"
}

run_guard mounted rw
if run_guard not-mounted rw; then
  echo 'directory without a matching mount must be rejected' >&2
  exit 1
fi
if run_guard mounted ro; then
  echo 'read-only mount must be rejected' >&2
  exit 1
fi
if PATH="$fake_bin:$PATH" FAKE_FINDMNT_TARGET="$mount_path" FAKE_FINDMNT_UUID="other-uuid" FAKE_FINDMNT_MODE=mounted FAKE_FINDMNT_OPTIONS=rw bash "$script" "$mount_path" "test-uuid"; then
  echo 'wrong UUID must be rejected' >&2
  exit 1
fi
if PATH="$fake_bin:$PATH" FAKE_FINDMNT_TARGET="$mount_path" FAKE_FINDMNT_UUID="test-uuid" FAKE_FINDMNT_MODE=wrong-target FAKE_FINDMNT_OPTIONS=rw bash "$script" "$mount_path" "test-uuid"; then
  echo 'a parent/root mount must not satisfy the guard' >&2
  exit 1
fi

echo 'mount guard tests passed'
