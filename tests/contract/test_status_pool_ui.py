"""Execute dashboard rendering to catch null-as-zero and cross-pool regressions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def evaluate_chart(payload):
    node = shutil.which('node') or shutil.which('node.exe')
    if not node:
        pytest.skip('Node required for browser rendering contract')
    html = Path('services/telemetry/src/homeserver_telemetry/templates/status.html').read_text()
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]
    program = (
        'global.fetch=()=>new Promise(()=>{}); global.setInterval=()=>{};\n'
        + script + '\nconsole.log(JSON.stringify(poolChart(' + json.dumps(payload) + ')));'
    )
    result = subprocess.run([node], input=program, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_pool_chart_keeps_physical_categories_free_and_reserved_proportions():
    chart = evaluate_chart(dict(state='ready', total_bytes=1000, used_bytes=600,
                                free_bytes=350, storage=dict(state='ok', movies_bytes=200,
                                series_bytes=100, torrents_bytes=200, other_bytes=100)))
    assert chart['breakdown'] is True
    assert chart['segments'] == dict(movies=20, series=10, torrents=20, other=10,
                                     used=0, free=35, reserved=5)
    assert sum(chart['segments'].values()) == pytest.approx(100)


@pytest.mark.parametrize('storage', [None, {'state': 'stale', 'movies_bytes': 500},
                                      {'state': 'ok', 'movies_bytes': 500},
                                      {'state': 'ok', 'movies_bytes': 700, 'series_bytes': 0,
                                       'torrents_bytes': 0, 'other_bytes': 0}])
def test_unknown_or_invalid_breakdown_uses_actual_used_without_fake_categories(storage):
    chart = evaluate_chart(dict(state='ready', total_bytes=1000, used_bytes=600,
                                free_bytes=350, storage=storage))
    assert chart['breakdown'] is False
    assert chart['segments'] == dict(movies=0, series=0, torrents=0, other=0,
                                     used=60, free=35, reserved=5)


@pytest.mark.parametrize('pool', [dict(state='unavailable'),
                                  dict(state='stale', total_bytes=1000,
                                       used_bytes=600, free_bytes=350),
                                  dict(state='ready', total_bytes=0, used_bytes=0, free_bytes=0),
                                  dict(state='ready', total_bytes=1000,
                                       used_bytes=None, free_bytes=350),
                                  dict(state='ready', total_bytes=1000,
                                       used_bytes=700, free_bytes=400)])
def test_unavailable_stale_and_inconsistent_pool_cannot_produce_a_zero_graph(pool):
    assert evaluate_chart(pool) is None


def test_partial_breakdown_preserves_unclassified_used_bytes():
    chart = evaluate_chart(dict(state='ready', total_bytes=1000, used_bytes=600,
                                free_bytes=350, storage=dict(state='ok', movies_bytes=200,
                                series_bytes=100, torrents_bytes=100, other_bytes=100)))
    assert chart['segments']['used'] == 10
    assert sum(chart['segments'].values()) == pytest.approx(100)


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
  firstElementChild: {style:{}}, attributes: {},
  setAttribute(key,value){this.attributes[key]=value;},
  removeAttribute(key){delete this.attributes[key];}
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
                free_bytes=3500,
                queue_remaining_bytes=1000,
                admissible_bytes=3000,
                storage=dict(state='ok', movies_bytes=2000, series_bytes=1000,
                             torrents_bytes=2000, other_bytes=1000),
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
const actual = {ssd: elements['pool-ssd-free'].textContent,
 hdd: elements['pool-hdd-free'].textContent,
 queue: elements['pool-hdd-queue'].textContent,
 cpu: elements.cpu.textContent, hidden: elements['legacy-storage'].hidden,
 poolsHidden: elements['pool-storage'].hidden, live: elements['live-text'].textContent,
 freeBar: elements['pool-ssd-free-bar'].style.width,
 reservedBar: elements['pool-ssd-reserved-bar'].style.width,
 moviesBar: elements['pool-ssd-movies-bar'].style.width,
 ssdChartHidden: elements['pool-ssd-chart'].hidden,
 hddChartHidden: elements['pool-hdd-chart'].hidden,
 graphLabel: elements['pool-ssd-chart'].attributes['aria-label']};
renderPools([{pool_id:'ssd', state:'stale'}]);
actual.staleChartHidden = elements['pool-ssd-chart'].hidden;
actual.staleMoviesBar = elements['pool-ssd-movies-bar'].style.width;
actual.staleBreakdownHidden = elements['pool-ssd-breakdown'].hidden;
console.log(JSON.stringify(actual));
"""
    )
    result = subprocess.run([node], input=program, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual["ssd"] != "—"
    assert actual["hdd"] == "—"
    assert actual["queue"] == "—"
    assert actual["cpu"] == "42%"
    assert actual["hidden"] is True
    assert actual["poolsHidden"] is False
    assert actual["live"] == "Online"
    assert actual['freeBar'] == '35%'
    assert actual['reservedBar'] == '5%'
    assert actual['moviesBar'] == '20%'
    assert actual['ssdChartHidden'] is False
    assert actual['hddChartHidden'] is True
    assert 'Filmes 20%' in actual['graphLabel']
    assert 'Livre 35%' in actual['graphLabel']
    assert 'Reservado / indisponível 5%' in actual['graphLabel']
    assert actual['staleChartHidden'] is True
    assert actual['staleMoviesBar'] == '0%'
    assert actual['staleBreakdownHidden'] is True
