import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def local_tmp():
    path = Path(".runtime") / f"test-layout-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_hardlink_preserves_payload_after_unlink(local_tmp: Path) -> None:
    tmp_path = local_tmp
    original = tmp_path / "download.mkv"
    imported = tmp_path / "library.mkv"
    original.write_bytes(b"fixture")
    os.link(original, imported)
    assert original.stat().st_ino == imported.stat().st_ino
    assert original.stat().st_dev == imported.stat().st_dev
    original.unlink()
    assert imported.read_bytes() == b"fixture"


def test_dev_fixture_keeps_host_media_and_appdata_isolated(tmp_path) -> None:
    from homeserver_common.env import load_settings
    from homeserver_common.render import render_stack

    env_file = tmp_path / ".env"
    env_file.write_text("")
    stack = render_stack(load_settings(env_file, mode="dev"), env_file=env_file)
    for service in stack["services"].values():
        for mount in service["volumes"]:
            assert not mount["source"].startswith("/srv/")
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1"
