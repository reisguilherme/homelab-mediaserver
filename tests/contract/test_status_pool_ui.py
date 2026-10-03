"""Execute dashboard rendering to catch null-as-zero and cross-pool regressions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("capacity_state", ["ok", "unavailable"])
def test_dashboard_renders_independent_physical_pool_cards(capacity_state):
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("Node required for browser rendering contract")
    html = Path("services/telemetry/src/homeserver_telemetry/templates/status.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = """
const elements = Object.fromEntries([...HTML.matchAll(/id="([^"]+)"/g)].map(m => [m[1], {
  textContent: '', hidden: false, dataset: {}, style: {setProperty(){}},
  firstElementChild: {style:{}}, setAttribute(){}, removeAttribute(){}
}]));
global.document = {getElementById: id => {if(!elements[id]) throw Error(id); return elements[id];}};
global.fetch = () => new Promise(()=>{});
global.setInterval = ()=>{};
""".replace("HTML", json.dumps(html))
    payload = {
        "host": {"state": "ok", "cpu_percent": 42},
        "capacity": {"state": capacity_state},
        "pools": [
            dict(
                pool_id="ssd",
                state="ready",
                total_bytes=10000,
                used_bytes=6000,
                free_bytes=4000,
                queue_remaining_bytes=1000,
                admissible_bytes=3000,
            ),
            dict(
                pool_id="hdd",
                state="unavailable",
                total_bytes=None,
                used_bytes=None,
                free_bytes=None,
                queue_remaining_bytes=None,
                admissible_bytes=None,
            ),
        ],
    }
    program = (
        harness
        + script
        + "\nrender("
        + json.dumps(payload)
        + """);
console.log(JSON.stringify({ssd: elements['pool-ssd-free'].textContent,
 hdd: elements['pool-hdd-free'].textContent,
 queue: elements['pool-hdd-queue'].textContent,
 cpu: elements.cpu.textContent, hidden: elements['legacy-storage'].hidden,
 poolsHidden: elements['pool-storage'].hidden, live: elements['live-text'].textContent}));
"""
    )
    result = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True)
    actual = json.loads(result.stdout)
    assert actual["ssd"] != "—"
    assert actual["hdd"] == "—"
    assert actual["queue"] == "—"
    assert actual["cpu"] == "42%"
    assert actual["hidden"] is True
    assert actual["poolsHidden"] is False
    assert actual["live"] == "Online"
