#!/usr/bin/env python3
"""Read host metrics or verify a running personal Docker Compose stack."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/common/src"))

from homeserver_common.env import load_settings, parse_env  # noqa: E402
from homeserver_common.host import DependencyError, preflight, run_checked  # noqa: E402
from homeserver_common.render import atomic_write  # noqa: E402


def metrics(settings):
    preflight(settings, host_tools=False)
    run = Path(settings.run_root)
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
            media_path=Path(settings.media_root),
            previous=previous,
            network_interface=settings.network_interface,
        ),
    )
    usage = os.statvfs(settings.media_root)
    atomic_write(
        run / "capacity.json",
        json.dumps(
            {
                "filesystem_id": settings.media_uuid or "dev-fixture",
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("metrics", "smoke", "layout"))
    parser.add_argument("--env-file", "--config", type=Path, default=ROOT / ".env")
    parser.add_argument("--environment", choices=("dev", "prod"))
    args = parser.parse_args(argv)
    try:
        declared = parse_env(args.env_file.read_text()).get("HOMESERVER_ENVIRONMENT", "prod")
        settings = load_settings(args.env_file, mode=args.environment or declared)
        if args.action == "metrics":
            result = metrics(settings)
        elif args.action == "smoke":
            preflight(settings)
            address = (
                settings.tailscale_bind_ip
                if settings.access_mode in ("tailscale", "both")
                else settings.lan_bind_ip
            )
            address = f"[{address}]" if ":" in address else address
            with urlopen(
                f"http://{address}:{settings.control_port}/health/ready",
                timeout=settings.http_timeout_seconds,
            ) as response:
                if response.status != 200:
                    raise RuntimeError("readiness check failed")
            result = {"ready": True}
        else:
            preflight(settings)
            run_checked(
                [
                    "bash",
                    str(ROOT / "scripts/verify-layout.sh"),
                    "--environment",
                    settings.environment,
                ],
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOMESERVER_LAYOUT_ROOT": settings.media_root,
                    "HOMESERVER_MEDIA_UUID": settings.media_uuid,
                },
            )
            result = {"hardlinks": "verified"}
        print(json.dumps(result, sort_keys=True))
        return 0
    except DependencyError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ValueError, OSError):
        print("host check failed: invalid configuration or unavailable file", file=sys.stderr)
        return 2
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
