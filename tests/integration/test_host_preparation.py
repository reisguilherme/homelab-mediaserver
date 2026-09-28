from homeserver_common.env import load_settings


def test_runtime_preparation_seeds_missing_native_configs_without_replacing_them(tmp_path):
    from homeserver_common.host import prepare_runtime

    env = tmp_path / ".env"
    env.write_text(
        f"HOMESERVER_APPDATA_ROOT={tmp_path / 'appdata'}\n"
        f"HOMESERVER_MEDIA_ROOT={tmp_path / 'media'}\n"
        f"HOMESERVER_TRANSCODE_ROOT={tmp_path / 'transcode'}\n"
        f"HOMESERVER_RUN_ROOT={tmp_path / 'run'}\n"
        "HOMESERVER_SONARR_API_KEY=fixture-key\n"
        "HOMESERVER_QBIT_PASSWORD=fixture-password\n"
    )
    settings = load_settings(env, mode="dev")
    result = prepare_runtime(settings)
    config = tmp_path / "appdata/sonarr/config.xml"
    assert config.is_file()
    assert result["seeded"] == 2
    config.write_text("native app now owns this file")
    qbit = tmp_path / "appdata/qbittorrent/qBittorrent/qBittorrent.conf"
    original_qbit = qbit.read_bytes()
    assert prepare_runtime(settings)["seeded"] == 0
    assert config.read_text() == "native app now owns this file"
    assert qbit.read_bytes() == original_qbit
    assert (tmp_path / "media/media/tv").is_dir()
    assert not (tmp_path / "releases").exists()


def test_runtime_preparation_rejects_symlinked_native_config(tmp_path):
    import pytest

    from homeserver_common.host import prepare_runtime

    env = tmp_path / ".env"
    env.write_text(
        f"HOMESERVER_APPDATA_ROOT={tmp_path / 'appdata'}\n"
        f"HOMESERVER_MEDIA_ROOT={tmp_path / 'media'}\n"
        f"HOMESERVER_TRANSCODE_ROOT={tmp_path / 'transcode'}\n"
        f"HOMESERVER_RUN_ROOT={tmp_path / 'run'}\n"
        "HOMESERVER_SONARR_API_KEY=fixture-key\n"
    )
    config = tmp_path / "appdata/sonarr/config.xml"
    config.parent.mkdir(parents=True)
    target = tmp_path / "operator-owned"
    target.write_text("keep")
    config.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        prepare_runtime(load_settings(env, mode="dev"))
    assert target.read_text() == "keep"


def test_init_container_uses_its_fixed_bind_paths(monkeypatch, tmp_path):
    from homeserver_common import host

    env = tmp_path / ".env"
    env.write_text("")
    seen = []
    monkeypatch.setenv("HOMESERVER_NATIVE_CONTEXT", "container")
    monkeypatch.setattr(host, "prepare_runtime", lambda settings: seen.append(settings) or {})
    assert host.main(["--env-file", str(env), "--mode", "dev"]) == 0
    assert seen[0].appdata_root == "/srv/appdata"
    assert seen[0].media_root == "/srv/data"
    assert seen[0].transcode_root == "/srv/transcode"
    assert seen[0].run_root == "/run/homeserver"


def test_seed_preparation_does_not_require_the_jellyfin_gpu_device(tmp_path):
    from homeserver_common.host import preflight, prepare_runtime

    env = tmp_path / ".env"
    env.write_text(
        f"HOMESERVER_APPDATA_ROOT={tmp_path / 'appdata'}\n"
        f"HOMESERVER_MEDIA_ROOT={tmp_path / 'media'}\n"
        f"HOMESERVER_TRANSCODE_ROOT={tmp_path / 'transcode'}\n"
        f"HOMESERVER_RUN_ROOT={tmp_path / 'run'}\n"
        "HOMESERVER_TRANSCODE_MODE=intel\n"
        f"HOMESERVER_INTEL_RENDER_DEVICE={tmp_path / 'missing-render-device'}\n"
    )
    settings = load_settings(env, mode="dev")
    assert prepare_runtime(settings)["prepared"]
    import pytest

    with pytest.raises(ValueError, match="Intel render device"):
        preflight(settings)
