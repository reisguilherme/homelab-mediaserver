#!/usr/bin/env bash
# Deprecated implementation: bootstrap-server.sh now delegates to the typed installer.
# No inventory, environment file or command text is executed by this helper.
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/python.sh" install "$@"
