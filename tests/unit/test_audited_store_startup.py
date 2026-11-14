"""Startup avoids replaying historical jobs unless a legacy binding is missing."""

import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from test_audited_import_store import appended_manifest, manifest

from homeserver_control.persistence import audited_imports as api


def test_bound_database_never_reads_or_decodes_full_history_on_startup(tmp_path, monkeypatch):
    db = tmp_path / "control.sqlite"
    store = api.AuditedImportStore(db)
    store.install_manifest(manifest())
    store.install_manifest(appended_manifest())
    statements = []

    class Traced(api.AuditedImportStore):
        @contextmanager
        def _connect(self):
            with super()._connect() as connection:
                connection.set_trace_callback(statements.append)
                yield connection

    def forbidden(_content):
        raise AssertionError("history JSON replayed on fully bound database")

    monkeypatch.setattr(api, "json", SimpleNamespace(loads=forbidden, dumps=json.dumps))
    Traced(db)
    assert not any("SELECT manifest_id, manifest_json FROM" in sql for sql in statements)
    assert not any("UPDATE audited_import_jobs SET manifest_id=" in sql for sql in statements)


def test_missing_legacy_binding_recovers_first_sealed_manifest(tmp_path):
    db = tmp_path / "control.sqlite"
    store = api.AuditedImportStore(db)
    first = manifest()
    second = appended_manifest()
    store.install_manifest(first)
    store.install_manifest(second)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE audited_import_jobs SET manifest_id=NULL")
    api.AuditedImportStore(db)
    with sqlite3.connect(db) as c:
        rows = dict(c.execute("SELECT job_id,manifest_id FROM audited_import_jobs"))
    assert rows[first["jobs"][0]["job_id"]] == first["manifest_id"]
    assert rows[second["jobs"][-1]["job_id"]] == second["manifest_id"]
