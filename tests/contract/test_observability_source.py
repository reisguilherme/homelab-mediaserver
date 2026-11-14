"""The source override opts only the existing collector into sanitized publication."""

from pathlib import Path

import yaml


def test_source_override_adds_dedicated_mount_group_and_preserves_collector_command():
    base = yaml.safe_load(Path("compose.yaml").read_text())
    override = yaml.safe_load(Path("compose.observability-source.yaml").read_text())

    assert set(override) == {"services"}
    assert set(override["services"]) == {"host-metrics"}
    source = override["services"]["host-metrics"]
    assert set(source) == {"command", "volumes", "group_add"}
    assert source["group_add"] == ["65534"]
    assert source["command"] == base["services"]["host-metrics"]["command"] + [
        "--metrics-output-dir", "/run/homeserver-metrics",
    ]
    assert source["volumes"] == [{
        "type": "bind",
        "source": "${HOMESERVER_OBSERVABILITY_DIR:-/srv/appdata/observability}/snapshots",
        "target": "/run/homeserver-metrics", "read_only": False,
        "bind": {"create_host_path": False},
    }]
    assert all(volume["target"] != "/run/homeserver-metrics"
               for volume in base["services"]["host-metrics"]["volumes"])
    assert "--metrics-output-dir" not in base["services"]["host-metrics"]["command"]
