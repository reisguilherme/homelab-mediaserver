"""Configuration operator for the personal Docker Compose stack."""

import argparse
import asyncio
import dataclasses
import errno
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from .catalog import catalog, example_env
from .env import load_settings, parse_env, serialize_env
from .settings import FIELDS


def _private_write(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.stat() if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=".homeserver-private-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, original.st_mode & 0o777 if original else 0o600)
        if original and os.geteuid() == 0:
            os.chown(temporary, original.st_uid, original.st_gid)
        try:
            os.replace(temporary, path)
        except OSError as error:
            if error.errno != errno.EBUSY:
                raise
            # File bind mounts cannot be replaced; normal project-directory
            # mounts retain the atomic rename above.
            with path.open("w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if not original:
                os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def native_configuration(
    settings, action, *, in_container=False, env_file=None, adopt_credentials=True
):
    if in_container:
        from .host import container_settings

        settings = container_settings(settings)
    from homeserver_control.configuration.reconcile import (
        ServiceOutcome,
        apply_native_settings,
        discover_native_credentials,
        plan_native_settings,
        verify_native_settings,
    )

    function = {
        "plan": plan_native_settings,
        "apply": apply_native_settings,
        "verify": verify_native_settings,
    }[action]

    async def execute():
        outcomes = await function(settings)
        updates = {}
        if action == "apply":
            try:
                updates = await discover_native_credentials(settings)
            except Exception:
                outcomes.append(
                    ServiceOutcome("credentials", "failed", message="credential discovery failed")
                )
        return outcomes, updates

    result, updates = asyncio.run(execute())
    outcomes = _validate_outcomes([dataclasses.asdict(item) for item in result])
    if updates and env_file is not None:
        allowed = {"HOMESERVER_JELLYFIN_API_KEY", "HOMESERVER_SEERR_API_KEY"}
        if (
            not isinstance(updates, dict)
            or set(updates) - allowed
            or any(not isinstance(token, str) or not token for token in updates.values())
        ):
            raise RuntimeError("invalid credential adoption record")
        original = parse_env(Path(env_file).read_text(encoding="utf-8"))
        if any(original.get(key) != token for key, token in updates.items()):
            _private_write(Path(env_file), serialize_env(original | updates))
            if adopt_credentials:
                return native_configuration(
                    load_settings(env_file, mode=settings.environment),
                    action,
                    in_container=in_container,
                    env_file=env_file,
                    adopt_credentials=False,
                )
    return outcomes


def _validate_outcomes(value):
    allowed = {"verified", "planned", "drift", "failed", "unsupported"}
    expected = {"qbittorrent", "sonarr", "radarr", "prowlarr", "bazarr", "jellyfin", "seerr"}
    if not isinstance(value, list) or not value:
        raise RuntimeError("invalid operator outcome")
    seen = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or item.get("status") not in allowed
            or not isinstance(item.get("service"), str)
            or not isinstance(item.get("changes"), list)
            or not isinstance(item.get("message", ""), str)
        ):
            raise RuntimeError("invalid operator outcome")
        service = item["service"]
        if service in seen or service not in expected | {"credentials"}:
            raise RuntimeError("invalid operator outcome")
        if service == "credentials" and item["status"] != "failed":
            raise RuntimeError("invalid operator outcome")
        seen.add(service)
        for change in item["changes"]:
            if (
                not isinstance(change, dict)
                or change.get("service") != service
                or not isinstance(change.get("key"), str)
                or type(change.get("secret")) is not bool
                or type(change.get("restart_required")) is not bool
                or "before" not in change
                or "after" not in change
            ):
                raise RuntimeError("invalid operator outcome")
            if change["secret"]:
                change["before"] = change["after"] = "<redacted>"
    if not expected <= seen:
        raise RuntimeError("invalid operator outcome")
    return value


def initialize_env(path, mode):
    path = Path(path).resolve()
    values = parse_env(example_env())
    generated = {
        "arr_token",
        "admin_token",
        "csrf_token",
        "admin_password",
        "qbit_password",
        "sonarr_api_key",
        "radarr_api_key",
        "prowlarr_api_key",
        "bazarr_api_key",
    }
    for name, field in FIELDS.items():
        key = "HOMESERVER_" + name.upper()
        if field.secret and name in generated and key in values:
            values[key] = (
                secrets.token_hex(16) if name.endswith("_api_key") else secrets.token_urlsafe(32)
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".homeserver-env-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialize_env(values))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"env_file": str(path), "created": True, "mode": mode}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="homeserver")
    groups = parser.add_subparsers(dest="group", required=True)
    env_sub = groups.add_parser("env").add_subparsers(dest="action", required=True)
    init = env_sub.add_parser("init")
    init.add_argument("--env-file", type=Path, required=True)
    init.add_argument("--mode", choices=("dev", "prod"), default="prod")
    sub = groups.add_parser("config").add_subparsers(dest="action", required=True)
    for action in ("validate", "show", "catalog", "plan", "apply", "verify"):
        command = sub.add_parser(action)
        command.add_argument("--env-file", type=Path)
        command.add_argument("--mode", choices=("dev", "prod"), default="prod")
        if action == "show":
            command.add_argument("--redacted", action="store_true", required=True)
        if action == "catalog":
            command.add_argument("--example", action="store_true")
        if action in ("plan", "apply", "verify"):
            command.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    doctor = groups.add_parser("doctor")
    doctor.add_argument("--env-file", type=Path, required=True)
    doctor.add_argument("--mode", choices=("dev", "prod"), default="prod")
    args = parser.parse_args(argv)
    try:
        if args.group == "env":
            result = initialize_env(args.env_file, args.mode)
        elif args.group == "config" and args.action == "catalog":
            if args.example:
                print(example_env(), end="")
                return 0
            result = catalog()
        else:
            if args.env_file is None:
                parser.error("--env-file is required")
            try:
                settings = load_settings(args.env_file, mode=args.mode)
            except ValueError as error:
                print("configuration error: " + str(error), file=sys.stderr)
                return 2
            if args.group == "doctor":
                from .host import preflight

                result = {"checks": preflight(settings), "configuration": "verified"}
            elif args.action in ("plan", "apply", "verify"):
                result = native_configuration(
                    settings, args.action, in_container=args.in_container, env_file=args.env_file
                )
            else:
                result = settings.redacted_dict() if args.action == "show" else {"valid": True}
        print(json.dumps(result, indent=2))
        if args.group == "config" and args.action in ("plan", "apply", "verify"):
            if any(item.get("status") in ("failed", "unsupported") for item in result):
                return 4
            if args.action in ("apply", "verify") and any(
                item.get("status") != "verified" for item in result
            ):
                return 4
        return 0
    except (ValueError, FileExistsError):
        error = sys.exception()
        print(
            "configuration error: "
            + (
                "invalid operation argument"
                if isinstance(error, ValueError)
                else "env file already exists"
            ),
            file=sys.stderr,
        )
        return 2
    except OSError:
        print("configuration error: file unavailable", file=sys.stderr)
        return 2
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print("operation failed: " + type(error).__name__, file=sys.stderr)
        return 3 if type(error).__name__ == "DependencyError" else 4


if __name__ == "__main__":
    raise SystemExit(main())
