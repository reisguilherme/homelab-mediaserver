#!/usr/bin/env bash
set -euo pipefail

# Pin both the scanner and its verified official binary; diagnostics redact secrets.
scanner_version=8.28.0
if command -v gitleaks >/dev/null 2>&1; then
  scanner=$(command -v gitleaks)
  [[ "$($scanner version)" == "$scanner_version" ]] || {
    echo "gitleaks $scanner_version is required" >&2
    exit 3
  }
else
  case $(uname -s):$(uname -m) in
    Linux:x86_64) scanner_platform=linux_x64 ;;
    Linux:aarch64) scanner_platform=linux_arm64 ;;
    *) echo 'install gitleaks 8.28.0 on this platform' >&2; exit 3 ;;
  esac
  task_tools=$(mktemp -d)
  trap 'rm -rf -- "$task_tools"' EXIT
  base="https://github.com/gitleaks/gitleaks/releases/download/v$scanner_version"
  binary="gitleaks_${scanner_version}_${scanner_platform}.tar.gz"
  curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 "$base/$binary" -o "$task_tools/$binary"
  curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 "$base/gitleaks_${scanner_version}_checksums.txt" -o "$task_tools/checksums.txt"
  (cd "$task_tools" && awk -v name="$binary" '$2 == name {print}' checksums.txt | sha256sum --check --status)
  tar -xzf "$task_tools/$binary" -C "$task_tools" gitleaks
  scanner="$task_tools/gitleaks"
fi

root=$(git rev-parse --show-toplevel)
task_report=$(mktemp)
if ! "$scanner" git "$root" --redact --no-banner --config "$root/.gitleaks.toml" --log-opts='--all' --report-format=json --report-path="$task_report"; then
  python3 - "$task_report" <<'PY'
import json
import sys
for finding in json.load(open(sys.argv[1])):
    print(json.dumps({key: finding[key] for key in ("RuleID", "File", "StartLine", "Commit")}))
PY
  rm -f -- "$task_report"
  exit 1
fi
rm -f -- "$task_report"
"$scanner" dir "$root/services" --redact --no-banner --config "$root/.gitleaks.toml"
"$scanner" dir "$root/scripts" --redact --no-banner --config "$root/.gitleaks.toml"
"$scanner" dir "$root/deploy" --redact --no-banner --config "$root/.gitleaks.toml"
