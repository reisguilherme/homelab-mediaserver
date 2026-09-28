#!/usr/bin/env python3
"""Run the supervised Compose stack; maintenance always takes precedence."""

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
for service in ("common", "control", "telemetry"):
    sys.path.insert(0, str(root / "services" / service / "src"))

from homeserver_common.env import load_settings  # noqa: E402
from homeserver_common.supervision import StackSupervisor  # noqa: E402


def alert_delivery(settings, state):
    if not settings.alerts_enabled:
        return
    import httpx

    from homeserver_telemetry.alerts import NotificationOutbox
    from homeserver_telemetry.notifications import NotificationService

    folder = Path(settings.appdata_root) / "telemetry"
    folder.mkdir(parents=True, exist_ok=True)
    outbox = NotificationOutbox(str(folder / "notifications.sqlite"))

    def transport(payload):
        with httpx.Client(timeout=settings.http_timeout_seconds, trust_env=False) as client:
            client.post(settings.alert_webhook_url, json=payload).raise_for_status()

    service = NotificationService(
        outbox=outbox, transport=transport, retry_seconds=settings.alert_retry_seconds
    )
    try:
        from homeserver_common.backup_status import read_backup_status

        backup = read_backup_status(
            Path(settings.appdata_root) / "control/last-backup.json",
            enabled=settings.backup_enabled,
            stale_hours=settings.backup_stale_hours,
        )
        if backup["state"] in {"stale", "missing", "unavailable"}:
            service.enqueue(
                event_type="backup",
                object_id=settings.instance_name,
                generation=f"{backup['state']}:{backup['completed_at']}",
                payload={"instance": settings.instance_name, "backup": backup},
            )
        if state["status"] in {"blocked", "degraded"}:
            service.enqueue(
                event_type="stack",
                object_id=settings.instance_name,
                generation=str(int(time.time() // settings.supervisor_restart_window_seconds)),
                payload={
                    "instance": settings.instance_name,
                    "status": state["status"],
                    "services": state.get("services", {}),
                },
            )
        service.deliver(now=time.time())
    finally:
        outbox.connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = load_settings(args.env_file, mode="prod")
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    supervisor = StackSupervisor(settings)
    running = True

    def stop(_signal, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        try:
            state = supervisor.run_once()
            alert_delivery(settings, state)
            if args.once:
                print(json.dumps(state))
                return 0 if state["status"] == "ok" else 4
        except Exception as error:
            logging.error("stack supervision failed (%s)", type(error).__name__)
            if args.once:
                return 4
        time.sleep(settings.supervisor_interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
