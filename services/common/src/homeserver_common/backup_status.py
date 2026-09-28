"""Read verified backup evidence without exposing repository or snapshot data."""

import json
import math
import time
from pathlib import Path


def read_backup_status(path: Path, *, enabled: bool, stale_hours: float, now=None) -> dict:
    result = {"state": "disabled", "age_seconds": None, "completed_at": None}
    if not enabled:
        return result
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**result, "state": "missing"}
    except (OSError, ValueError):
        return {**result, "state": "unavailable"}
    completed = receipt.get("completed_at") if isinstance(receipt, dict) else None
    now = time.time() if now is None else now
    if (
        not isinstance(receipt, dict)
        or receipt.get("verified") is not True
        or type(completed) not in (int, float)
        or not math.isfinite(completed)
        or completed < 0
        or completed > now
    ):
        return {**result, "state": "unavailable"}
    age = now - completed
    return {
        "state": "stale" if stale_hours > 0 and age > stale_hours * 3600 else "ok",
        "age_seconds": age,
        "completed_at": completed,
    }
