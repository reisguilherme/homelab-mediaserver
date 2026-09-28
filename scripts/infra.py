#!/usr/bin/env python3
"""Compatibility entrypoint for shell tools; all configuration uses the shared loader."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/common/src"))

from homeserver_common.env import load_settings, parse_env  # noqa: E402
from homeserver_common.install import DependencyError  # noqa: E402


def metrics(settings):
    from homeserver_common.install import preflight
    from homeserver_common.render import atomic_write

    env = settings.as_environment()
    run = Path(env["HOMESERVER_RUN_ROOT"])
    preflight(settings, host_tools=False)
    spec = importlib.util.spec_from_file_location(
        "homeserver_host_metrics", ROOT / "scripts/host-metrics.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = run / "host.json"
    try:
        previous = json.loads(output.read_text())
    except (OSError, ValueError):
        previous = None
    module.write_atomic(
        output,
        module.collect(
            media_path=Path(env["HOMESERVER_MEDIA_ROOT"]),
            previous=previous,
            network_interface=settings.network_interface,
        ),
    )
    import os
    import time

    usage = os.statvfs(env["HOMESERVER_MEDIA_ROOT"])
    atomic_write(
        run / "capacity.json",
        json.dumps(
            {
                "filesystem_id": env["HOMESERVER_MEDIA_UUID"] or "dev-fixture",
                "free_bytes": usage.f_bavail * usage.f_frsize,
                "total_bytes": usage.f_blocks * usage.f_frsize,
                "measured_at": time.time(),
            }
        )
        + "\n",
        mode=0o640,
    )
    return {"host_snapshot": "verified", "capacity_snapshot": "verified"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "install",
            "render",
            "backup-create",
            "backup-verify",
            "backup-copy",
            "restore",
            "deploy",
            "rollback",
            "build",
            "metrics",
            "smoke",
            "layout",
        ),
    )
    parser.add_argument("--env-file", "--config", dest="env_file", type=Path)
    parser.add_argument("--environment", choices=("dev", "prod"))
    parser.add_argument("--mode", choices=("fresh", "adopt"), default="fresh")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true")
    group.add_argument("--apply", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("--unit-root", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--release")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--image-prefix")
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            from homeserver_common.release import build_release

            result = build_release(args.output or ROOT / "backup/releases", args.image_prefix or "")
        else:
            if args.env_file is None:
                raise ValueError("--env-file is required")
            args.env_file = args.env_file.resolve()
            declared = parse_env(args.env_file.read_text()).get("HOMESERVER_ENVIRONMENT", "prod")
            settings = load_settings(args.env_file, mode=args.environment or declared)
            if args.action == "install":
                from homeserver_common.install import apply_install, plan_install

                function = apply_install if args.apply else plan_install
                result = function(settings, args.env_file, mode=args.mode, unit_root=args.unit_root)
            elif args.action == "render":
                from homeserver_common.render import write_stack

                path = args.output or Path(settings.install_root) / "shared/compose.json"
                write_stack(settings, path)
                result = {"rendered": True, "path": str(path)}
            elif args.action.startswith("backup-"):
                from homeserver_common.backup import copy_backups, create_backup, verify_backup

                result = (
                    create_backup(settings, args.env_file)
                    if args.action == "backup-create"
                    else verify_backup(settings)
                    if args.action == "backup-verify"
                    else copy_backups(settings)
                )
            elif args.action == "restore":
                from homeserver_common.backup import restore_snapshot

                if not args.snapshot or args.target is None:
                    raise ValueError("--snapshot and --target are required")
                result = restore_snapshot(
                    settings, args.snapshot, args.target, isolated=args.isolated
                )
            elif args.action == "deploy":
                from homeserver_common.release import deploy_release

                if not args.release or not args.artifact or not args.manifest:
                    raise ValueError("--release, --artifact and --manifest are required")
                result = deploy_release(
                    settings, args.env_file, args.release, args.artifact, args.manifest
                )
            elif args.action == "rollback":
                from homeserver_common.release import rollback_release

                if not args.release:
                    raise ValueError("--release is required")
                if args.snapshot:
                    raise ValueError(
                        "rollback never restores a database implicitly; use isolated restore"
                    )
                result = rollback_release(settings, args.env_file, args.release)
            elif args.action == "metrics":
                result = metrics(settings)
            elif args.action == "smoke":
                from urllib.request import urlopen

                from homeserver_common.install import preflight
                from homeserver_common.release import _health_url

                preflight(settings)
                url = _health_url(settings)
                with urlopen(url, timeout=settings.http_timeout_seconds) as response:
                    if response.status != 200:
                        raise RuntimeError("readiness check failed")
                result = {"ready": True}
            else:
                from homeserver_common.install import preflight, run_checked

                preflight(settings)
                run_checked(
                    [
                        "bash",
                        str(ROOT / "scripts/verify-layout.sh"),
                        "--environment",
                        settings.environment,
                    ],
                    env={
                        "PATH": __import__("os").environ.get("PATH", ""),
                        "HOMESERVER_LAYOUT_ROOT": str(settings.media_root),
                        "HOMESERVER_MEDIA_UUID": settings.media_uuid,
                    },
                )
                result = {"hardlinks": "verified"}
        print(json.dumps(result, default=str, sort_keys=True))
        return 0
    except DependencyError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ValueError, OSError) as error:
        print(
            str(error) if isinstance(error, ValueError) else "infrastructure file operation failed",
            file=sys.stderr,
        )
        return 2
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
