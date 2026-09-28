"""Load literal personal preferences before replacing the container process."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from .env import load_settings
from .settings import FIELDS


def apply_runtime_configuration(
    path: Path | str, mode: str, environment: Mapping[str, str]
) -> dict[str, str]:
    """Apply preferences while retaining the container's fixed paths and service wiring."""
    settings = load_settings(Path(path), mode=mode)
    configured = settings.as_environment()
    result = dict(environment)
    for name, field in FIELDS.items():
        if field.editable:
            key = "HOMESERVER_" + name.upper()
            result[key] = configured[key]
    result["HOMESERVER_WORKER_INTERVAL"] = configured["HOMESERVER_WORKER_INTERVAL"]
    return result


def _configured_command(arguments: list[str], environment: Mapping[str, str]) -> list[str]:
    command = list(arguments)
    level = environment["HOMESERVER_LOG_LEVEL"].lower()
    for index, argument in enumerate(command):
        if argument == "--log-level":
            if index + 1 == len(command):
                raise ValueError("--log-level: missing argument")
            command[index + 1] = level
        elif argument.startswith("--log-level="):
            command[index] = "--log-level=" + level
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("Usage: python -m homeserver_common.runtime COMMAND [ARGS...]", file=sys.stderr)
        return 2
    try:
        environment = apply_runtime_configuration(
            os.environ.get("HOMESERVER_ENV_PATH", "/project/.env"),
            os.environ.get("HOMESERVER_ENV_MODE", "prod"),
            os.environ,
        )
        command = _configured_command(arguments, environment)
    except ValueError as error:
        print(f"Runtime configuration error: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("Runtime configuration error: .env unavailable", file=sys.stderr)
        return 2
    try:
        os.execvpe(command[0], command, environment)
    except OSError:
        print("Runtime command unavailable", file=sys.stderr)
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
