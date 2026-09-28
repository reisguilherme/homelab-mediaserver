from homeserver_control.gateway import app as gateway


def test_protected_env_is_effective_before_worker_initializes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMESERVER_SOURCE_PROTECTED_HASHES", "a" * 40)
    monkeypatch.setattr(gateway, "_database_path", str(tmp_path / "control.sqlite"))
    monkeypatch.setattr(gateway, "_production_source_health", None)
    assert gateway._configured_protected_source("A" * 40)
    assert not gateway._configured_protected_source("b" * 40)


def test_persisted_protection_remains_when_env_list_is_cleared(tmp_path, monkeypatch):
    from homeserver_control.worker.source_health import SourceHealthStore

    path = tmp_path / "control.sqlite"
    SourceHealthStore(path).protect_source("b" * 40)
    monkeypatch.setenv("HOMESERVER_SOURCE_PROTECTED_HASHES", "")
    monkeypatch.setattr(gateway, "_database_path", str(path))
    monkeypatch.setattr(gateway, "_production_source_health", SourceHealthStore(path))
    assert gateway._configured_protected_source("b" * 40)


def test_protected_read_does_not_take_write_lock_during_admission(tmp_path, monkeypatch):
    import sqlite3

    from homeserver_control.worker.source_health import SourceHealthStore

    path = tmp_path / "control.sqlite"
    store = SourceHealthStore(path)
    store.protect_source("b" * 40)
    monkeypatch.setenv("HOMESERVER_SOURCE_PROTECTED_HASHES", "")
    monkeypatch.setattr(gateway, "_production_source_health", store)
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert gateway._configured_protected_source("b" * 40)
