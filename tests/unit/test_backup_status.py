import json

import pytest

from homeserver_common.backup_status import read_backup_status


@pytest.mark.parametrize("receipt,state", [
    ({"verified": True, "completed_at": 1000}, "ok"),
    ({"verified": False, "completed_at": 1000}, "unavailable"),
    ({"verified": True, "completed_at": 20000}, "unavailable"),
    ({"verified": True, "completed_at": 0}, "stale"),
])
def test_backup_status_checks_evidence_and_age(tmp_path, receipt, state):
    path = tmp_path / "last-backup.json"
    path.write_text(json.dumps(receipt))
    status = read_backup_status(path, enabled=True, stale_hours=1, now=4000)
    assert status["state"] == state
    assert set(status) == {"state", "age_seconds", "completed_at"}


def test_backup_status_missing_disabled_and_corrupt(tmp_path):
    path = tmp_path / "last-backup.json"
    assert read_backup_status(path, enabled=False, stale_hours=1)["state"] == "disabled"
    assert read_backup_status(path, enabled=True, stale_hours=1)["state"] == "missing"
    path.write_text("not JSON")
    assert read_backup_status(path, enabled=True, stale_hours=1)["state"] == "unavailable"
