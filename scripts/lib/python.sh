#!/usr/bin/env bash
# Execute Python from this checkout, independent of the caller's working directory.
set -euo pipefail
homeserver_script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
homeserver_project_root=$(cd "$homeserver_script_root/.." && pwd)
if [[ -x "$homeserver_project_root/.venv/bin/python" ]]; then
  exec "$homeserver_project_root/.venv/bin/python" "$homeserver_script_root/infra.py" "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run --project "$homeserver_project_root" --frozen python "$homeserver_script_root/infra.py" "$@"
else
  exec python3 "$homeserver_script_root/infra.py" "$@"
fi
