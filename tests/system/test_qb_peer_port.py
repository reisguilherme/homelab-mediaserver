from pathlib import Path

import yaml


def test_only_torrent_peer_ports_are_published():
    qbit = yaml.safe_load(Path("compose.yaml").read_text())["services"]["qbittorrent"]
    assert {(p["host_ip"],int(p["published"]),p["target"],p["protocol"])
            for p in qbit["ports"]} == {
        ("0.0.0.0",6881,6881,"tcp"), ("0.0.0.0",6881,6881,"udp"),
    }
    assert qbit["environment"]["TORRENTING_PORT"] == "6881"
