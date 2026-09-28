from pathlib import Path

import pytest

from homeserver_common.env import load_settings
from homeserver_common.install import apply_install, plan_install


def installation(tmp_path: Path, extra=""):
    env = tmp_path / ".env"
    env.write_text(
        f"HOMESERVER_ENVIRONMENT=dev\nHOMESERVER_INSTALL_ROOT={tmp_path / 'install'}\n" + extra
    )
    return env, load_settings(env, mode="dev")


def test_plan_does_not_create_directories_and_second_apply_is_empty(tmp_path: Path) -> None:
    env, settings = installation(tmp_path)
    units = tmp_path / "units"
    planned = plan_install(settings, env, unit_root=units)
    assert planned["changes"]
    assert not Path(settings.install_root).exists()
    first = apply_install(settings, env, unit_root=units)
    assert first["changes"]
    assert (Path(settings.media_root) / "torrents").is_dir()
    assert (Path(settings.media_root) / "media").is_dir()
    assert (Path(settings.appdata_root) / "seerr").is_dir()
    assert (Path(settings.appdata_root) / "prowlarr").is_dir()
    assert apply_install(settings, env, unit_root=units)["changes"] == []
    assert plan_install(settings, env, mode="adopt", unit_root=units)["changes"] == []
    unit = (units / "homeserver-stack.service").read_text()
    assert str(env.resolve()) in unit


def test_adopt_preserves_unknown_generated_file(tmp_path: Path) -> None:
    env, settings = installation(tmp_path)
    shared = Path(settings.install_root) / "shared"
    shared.mkdir(parents=True)
    unknown = shared / "compose.json"
    unknown.write_text("operator-owned\n")
    with pytest.raises(ValueError, match="unmanaged"):
        apply_install(settings, env, mode="adopt", unit_root=tmp_path / "units")
    assert unknown.read_text() == "operator-owned\n"
    assert not Path(settings.appdata_root).exists()


def test_preflight_failure_creates_no_state(tmp_path: Path) -> None:
    env, settings = installation(
        tmp_path, "HOMESERVER_TRANSCODE_MODE=intel\nHOMESERVER_INTEL_RENDER_DEVICE=/missing/gpu\n"
    )
    with pytest.raises(ValueError, match="render device"):
        apply_install(settings, env, unit_root=tmp_path / "units")
    assert not Path(settings.install_root).exists()
    assert not Path(settings.appdata_root).exists()


def test_first_install_seeds_known_native_credentials_and_preserves_native_edits(
    tmp_path: Path,
) -> None:
    import base64
    import hashlib
    import re
    import xml.etree.ElementTree as xml

    import yaml

    env, settings = installation(
        tmp_path,
        "HOMESERVER_SONARR_API_KEY=sonarr-key\nHOMESERVER_RADARR_API_KEY=radarr-key\nHOMESERVER_PROWLARR_API_KEY=prowlarr-key\nHOMESERVER_BAZARR_API_KEY=bazarr-key\nHOMESERVER_QBIT_PASSWORD=initial-password\n",
    )
    apply_install(settings, env, unit_root=tmp_path / "units")
    appdata = Path(settings.appdata_root)
    for name in ("sonarr", "radarr", "prowlarr"):
        assert xml.parse(appdata / name / "config.xml").findtext("ApiKey") == name + "-key"
    assert (
        yaml.safe_load((appdata / "bazarr/config/config.yaml").read_text())["auth"]["apikey"]
        == "bazarr-key"
    )
    qbit = appdata / "qbittorrent/qBittorrent/qBittorrent.conf"
    encoded = re.search(r'Password_PBKDF2="@ByteArray\(([^)]+)\)"', qbit.read_text()).group(1)
    salt, digest = (base64.b64decode(part) for part in encoded.split(":"))
    assert len(salt) == 16 and len(digest) == 64
    assert hashlib.pbkdf2_hmac("sha512", b"initial-password", salt, 100000) == digest
    assert "[Network]\nPortForwardingEnabled=false" in qbit.read_text()
    assert "Connection\\UPnP=false" in qbit.read_text()
    assert "WebUI\\UseUPnP=false" in qbit.read_text()
    sonarr = appdata / "sonarr/config.xml"
    sonarr.write_text(sonarr.read_text().replace("sonarr-key", "native-changed-key"))
    before = qbit.read_bytes()
    assert apply_install(settings, env, mode="adopt", unit_root=tmp_path / "units")["changes"] == []
    assert "native-changed-key" in sonarr.read_text()
    assert qbit.read_bytes() == before
