#!/usr/bin/env bash
# Public image contents only. No runtime inventory, environment or Docker socket mounts.
set -Eeuo pipefail
umask 077
usage() { echo 'usage: check-images.sh [--output-dir DIRECTORY] IMAGE_REF [IMAGE_REF ...]' >&2; }
output=.runtime/image-security
if [[ ${1:-} == --output-dir ]]; then
  [[ $# -ge 3 ]] || { usage; exit 2; }
  output=$2
  shift 2
fi
[[ $# -gt 0 ]] || { usage; exit 2; }
for image in "$@"; do
  [[ "$image" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$ ]] || { usage; exit 2; }
done
for command in docker curl sha256sum tar python3 timeout; do
  command -v "$command" >/dev/null || { echo "image scan unknown: missing $command" >&2; exit 3; }
done
case $(uname -s):$(uname -m) in
  Linux:x86_64) platform=Linux-64bit; checksum=2edd39da482bb4e9831962487b68f68e3928ec3137794757f54d00383d79547b ;;
  Linux:aarch64) platform=Linux-ARM64; checksum=13833d97e8a1a5367471c372a173180157f593bece570e20d5d925fef552f5dd ;;
  *) echo 'image scan unknown: supported scanner platforms Linux x86_64/ARM64' >&2; exit 3 ;;
esac
# Official v0.73.0 release checksums, verified against its published checksum manifest.
# Deliberately excludes the compromised 0.69.4 and mutable scanner/action tags.
version=0.73.0
task_scan=$(mktemp -d)
trap 'rm -rf -- "$task_scan"' EXIT
trap 'echo "image scan unknown: scanner/export/database operation failed" >&2; exit 3' ERR
mkdir -p -- "$output" "$task_scan/home" "$task_scan/cache"
printf '{}\n' > "$task_scan/trivy.yaml"
output=$(cd "$output" && pwd)
printf '{"state":"unknown","reason":"scan incomplete"}\n' > "$output/scan-status.json"
binary="trivy_${version}_${platform}.tar.gz"
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  --connect-timeout 15 --max-time 180 --retry 2 \
  "https://github.com/aquasecurity/trivy/releases/download/v$version/$binary" -o "$task_scan/$binary"
printf '%s  %s\n' "$checksum" "$task_scan/$binary" | sha256sum --check --status
verification=checksum
if command -v cosign >/dev/null; then
  curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
    --connect-timeout 15 --max-time 60 --retry 2 \
    "https://github.com/aquasecurity/trivy/releases/download/v$version/$binary.sigstore.json" \
    -o "$task_scan/$binary.sigstore.json"
  verification_flags=()
  if cosign version 2>&1 | grep -Eq 'GitVersion:[[:space:]]+v2\.'; then
    verification_flags+=(--new-bundle-format)
  fi
  timeout 120 cosign verify-blob "$task_scan/$binary" \
    --bundle "$task_scan/$binary.sigstore.json" "${verification_flags[@]}" \
    --certificate-oidc-issuer=https://token.actions.githubusercontent.com \
    --certificate-identity "https://github.com/aquasecurity/trivy/.github/workflows/reusable-release.yaml@refs/tags/v$version"
  verification="checksum-and-sigstore"
fi
tar -xzf "$task_scan/$binary" -C "$task_scan" trivy
scanner="$task_scan/trivy"
# An empty environment/config/home prevents ambient credentials, plugins, ignore files,
# registry credentials and scanner configuration from changing the result.
scan() {
  env -i PATH=/usr/bin:/bin HOME="$task_scan/home" \
    timeout 600 "$scanner" --config "$task_scan/trivy.yaml" --cache-dir "$task_scan/cache" "$@"
}
scan image --download-db-only --no-progress
index=0
status=0
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
for image in "$@"; do
  index=$((index + 1))
  archive="$task_scan/image-$index.tar"
  # Resolve only the explicitly selected image; never enumerate containers or config.
  image_id=$(docker image inspect --format '{{.Id}}' "$image")
  [[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]]
  timeout 300 docker image save --output "$archive" "$image_id"
  report="$output/image-$index.json"
  scan image --input "$archive" --skip-db-update --offline-scan --scanners vuln \
    --ignorefile /dev/null --severity HIGH,CRITICAL --format json --output "$report" \
    --exit-code 0 --no-progress
  printf '{"image_ref":"%s","image_id":"%s","scanner":"trivy-%s","scanner_verification":"%s"}\n' \
    "$image" "$image_id" "$version" "$verification" > "$output/image-$index-inventory.json"
  if python3 "$root/scripts/image-scan-policy.py" "$report" > "$output/image-$index-summary.json"; then
    result=0
  else
    result=$?
  fi
  if [[ $result -eq 3 ]]; then
    echo 'image scan unknown: invalid vulnerability report' >&2
    exit 3
  fi
  [[ $result -eq 0 ]] || status=1
  cat "$output/image-$index-inventory.json" "$output/image-$index-summary.json"
  rm -f -- "$archive"
done
if [[ $status -eq 1 ]]; then critical=true; else critical=false; fi
printf '{"state":"scanned","images_scanned":%s,"fixable_critical_found":%s}\n' \
  "$index" "$critical" > "$output/scan-status.json"
exit "$status"
