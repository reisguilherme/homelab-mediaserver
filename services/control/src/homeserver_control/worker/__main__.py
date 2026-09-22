"""Run the control worker as ``python -m homeserver_control.worker``."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from homeserver_control.persistence.db import ReservationRepository


def main() -> None:
    database = Path(os.environ.get("HOMESERVER_DB_PATH", "/var/lib/homeserver/control.sqlite"))
    repository = ReservationRepository(database)
    repository.initialize()
    if os.environ.get("HOMESERVER_WORKER_ONCE") == "1":
        return

    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    interval = max(1.0, float(os.environ.get("HOMESERVER_WORKER_INTERVAL", "5")))
    while running:
        time.sleep(interval)


if __name__ == "__main__":
    main()
