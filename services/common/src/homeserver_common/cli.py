"""Operator entry point. Diagnostics never serialize credential values."""

import argparse
import asyncio
import dataclasses
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
    descriptor, temporary_name = tempfile.mkstemp(prefix=".homeserver-private-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def native_configuration(
    settings, action, *, in_container=False, env_file=None, adopt_credentials=True
):
    if settings.environment == "prod" and not in_container:
        from .install import write_operator_env

        compose = Path(settings.install_root) / "shared/compose.json"
        if not compose.is_file():
            raise ValueError("install apply must prepare the operator before native configuration")
        write_operator_env(settings)
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                settings.instance_name,
                "-f",
                str(compose),
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "operator",
                "config",
                action,
                "--env-file",
                "/run/homeserver/operator.env",
                "--in-container",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode:
            # Preserve the structured, redacted outcome when individual services failed.
            try:
                value = json.loads(result.stdout)
            except ValueError:
                raise RuntimeError("native configuration operator failed") from None
            if not isinstance(value, list):
                raise RuntimeError("invalid operator outcome")
        else:
            value = json.loads(result.stdout)
        value = _validate_outcomes(value)
        if result.returncode and all(item["status"] == "verified" for item in value):
            raise RuntimeError("native configuration operator failed")
        updates_file = Path(settings.run_root) / "native-credentials.json"
        if action == "apply" and updates_file.is_file():
            updates = json.loads(updates_file.read_text(encoding="utf-8"))
            allowed = {"HOMESERVER_JELLYFIN_API_KEY", "HOMESERVER_SEERR_API_KEY"}
            if (
                not isinstance(updates, dict)
                or set(updates) - allowed
                or any(not isinstance(token, str) or not token for token in updates.values())
            ):
                raise RuntimeError("invalid credential adoption record")
            if updates and env_file is not None:
                original = parse_env(Path(env_file).read_text(encoding="utf-8"))
                for key, token in updates.items():
                    original.pop(key + "_FILE", None)
                    original[key] = token
                _private_write(Path(env_file), serialize_env(original))
                from .install import write_operator_env

                write_operator_env(load_settings(env_file, mode=settings.environment))
            updates_file.unlink()
            if updates and env_file is not None and adopt_credentials:
                return native_configuration(
                    load_settings(env_file, mode=settings.environment),
                    action,
                    env_file=env_file,
                    adopt_credentials=False,
                )
        return value
    from homeserver_control.configuration.reconcile import (
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
        if action == "apply":
            try:
                updates = await discover_native_credentials(settings)
            except Exception:
                from homeserver_control.configuration.reconcile import ServiceOutcome

                outcomes.append(
                    ServiceOutcome("credentials", "failed", message="credential discovery failed")
                )
                return outcomes
            if updates:
                _private_write(
                    Path(settings.run_root) / "native-credentials.json", json.dumps(updates)
                )
        return outcomes

    outcomes = _validate_outcomes([dataclasses.asdict(item) for item in asyncio.run(execute())])
    if settings.environment == "dev" and action == "apply" and not in_container:
        updates_file = Path(settings.run_root) / "native-credentials.json"
        if updates_file.is_file() and env_file is not None:
            updates = json.loads(updates_file.read_text(encoding="utf-8"))
            allowed = {"HOMESERVER_JELLYFIN_API_KEY", "HOMESERVER_SEERR_API_KEY"}
            if (
                not isinstance(updates, dict)
                or set(updates) - allowed
                or any(not isinstance(token, str) or not token for token in updates.values())
            ):
                raise RuntimeError("invalid credential adoption record")
            original = parse_env(Path(env_file).read_text(encoding="utf-8"))
            for key, token in updates.items():
                original.pop(key + "_FILE", None)
                original[key] = token
            _private_write(Path(env_file), serialize_env(original))
            updates_file.unlink()
            if updates and adopt_credentials:
                return native_configuration(
                    load_settings(env_file, mode="dev"),
                    action,
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
            if change.get("secret"):
                change["before"] = "<redacted>"
                change["after"] = "<redacted>"
    if not expected <= seen:
        raise RuntimeError("invalid operator outcome")
    return value


def configure_native(settings, env_file: Path):
    """Host deployment hook: adopt first-use native keys and verify every applier."""
    outcomes = native_configuration(settings, "apply", env_file=env_file)
    if any(item.get("status") != "verified" for item in outcomes):
        raise RuntimeError("native settings were not verified")
    return load_settings(env_file, mode=settings.environment)


class _NativeVerificationFailed(RuntimeError):
    pass


def apply_host_configuration(settings, env_file):
    from .backup import maintenance, operation_lock
    from .release import refresh_runtime_configuration

    with operation_lock(settings):
        try:
            with maintenance(settings, "configure", keep_on_error=True, stop_stack=False):
                outcomes = native_configuration(settings, "apply", env_file=env_file)
                if any(item["status"] != "verified" for item in outcomes):
                    raise _NativeVerificationFailed("native settings were not verified")
                refresh_runtime_configuration(
                    load_settings(env_file, mode="prod"),
                    env_file,
                    _already_locked=True,
                    _already_maintenance=True,
                )
        except _NativeVerificationFailed:
            # Preserve the structured result while maintenance retains admission blocking.
            return outcomes
    return outcomes


def operation_result(args, settings):
    if args.group == "install":
        from .install import apply_install, plan_install

        function = plan_install if args.action == "plan" else apply_install
        return function(settings, args.env_file, mode=args.install_mode)
    if args.group == "backup":
        from .backup import copy_backups, create_backup, restore_snapshot, verify_backup

        if args.action == "create":
            return create_backup(settings, args.env_file)
        if args.action == "verify":
            return verify_backup(settings)
        if args.action == "copy":
            return copy_backups(settings)
        return restore_snapshot(settings, args.snapshot, args.target, isolated=True)
    if args.group == "render":
        from .render import render_stack

        _private_write(args.output, json.dumps(render_stack(settings), indent=2))
        return {"written": str(args.output)}
    if args.group == "doctor":
        from .install import preflight

        return {"checks": preflight(settings), "configuration": "verified"}
    raise ValueError("unknown operation")


def initialize_env(path, mode):
    path = Path(path).resolve()
    values = parse_env(example_env())
    values["HOMESERVER_ENVIRONMENT"] = mode
    for name, field in FIELDS.items():
        if field.secret and name in (
            "arr_token",
            "admin_token",
            "csrf_token",
            "admin_password",
            "qbit_password",
            "jellyfin_api_key",
            "seerr_api_key",
            "sonarr_api_key",
            "radarr_api_key",
            "prowlarr_api_key",
            "bazarr_api_key",
        ):
            values["HOMESERVER_" + name.upper()] = secrets.token_urlsafe(32)
    if mode == "dev":
        values["HOMESERVER_ACCESS_MODE"] = "lan"
        for name in (
            "install_root",
            "appdata_root",
            "media_root",
            "transcode_root",
            "backup_staging_root",
            "run_root",
        ):
            values["HOMESERVER_" + name.upper()] = str(
                path.parent / ".runtime/dev" / name.removesuffix("_root")
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish complete data atomically without replacing a file created concurrently.
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
    env_parser = groups.add_parser("env")
    env_sub = env_parser.add_subparsers(dest="action", required=True)
    init = env_sub.add_parser("init")
    init.add_argument("--env-file", type=Path, required=True)
    init.add_argument("--mode", choices=("dev", "prod"), default="prod")
    config = groups.add_parser("config")
    sub = config.add_subparsers(dest="action", required=True)
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
    install_parser = groups.add_parser("install")
    install_sub = install_parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply"):
        command = install_sub.add_parser(action)
        command.add_argument("--env-file", required=True, type=Path)
        command.add_argument("--environment", choices=("dev", "prod"), default="prod", dest="mode")
        command.add_argument(
            "--mode", choices=("fresh", "adopt"), default="fresh", dest="install_mode"
        )
    backup_parser = groups.add_parser("backup")
    backup_sub = backup_parser.add_subparsers(dest="action", required=True)
    for action in ("create", "verify", "copy", "restore"):
        command = backup_sub.add_parser(action)
        command.add_argument("--env-file", required=True, type=Path)
        command.add_argument("--mode", choices=("dev", "prod"), default="prod")
        if action == "restore":
            command.add_argument("--snapshot", required=True)
            command.add_argument("--target", required=True, type=Path)
    for group in ("render", "doctor"):
        command = groups.add_parser(group)
        command.add_argument("--env-file", required=True, type=Path)
        command.add_argument("--mode", choices=("dev", "prod"), default="prod")
        if group == "render":
            command.add_argument("--output", required=True, type=Path)
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
            if args.group == "config":
                if args.action in ("plan", "apply", "verify"):
                    locked = (
                        args.action == "apply"
                        and not args.in_container
                        and settings.environment == "prod"
                    )
                    if locked:
                        result = apply_host_configuration(settings, args.env_file)
                    else:
                        result = native_configuration(
                            settings,
                            args.action,
                            in_container=args.in_container,
                            env_file=args.env_file,
                        )
                else:
                    result = settings.redacted_dict() if args.action == "show" else {"valid": True}
            else:
                result = operation_result(args, settings)
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
        # Error messages from loader only identify keys; OSError paths can contain secrets.
        error = __import__("sys").exception()
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
