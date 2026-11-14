"""The landing view summarizes every physical disk without hiding missing data."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from test_third_pool_ui import run

HARNESS = """
const elements = Object.fromEntries([...HTML.matchAll(/id="([^"]+)"/g)].map(m => [m[1], {
  textContent:'', hidden:false, open:false, dataset:{}, style:{setProperty(){}},
  firstElementChild:{style:{}}, attributes:{},
  setAttribute(key,value){this.attributes[key]=value;},
  removeAttribute(key){delete this.attributes[key];}
}]));
global.document={getElementById:id=>{if(!elements[id]) throw Error(id); return elements[id];}};
const original=[
  {pool_id:'ssd',state:'ready',total_bytes:1000,used_bytes:900,free_bytes:80},
  {pool_id:'hdd',state:'ready',total_bytes:2000,used_bytes:1000,free_bytes:900},
  {pool_id:'hdd2',state:'ready',total_bytes:3000,used_bytes:1000,free_bytes:1800},
  {pool_id:'hdd3',label:'HD 18 TB',state:'ready',total_bytes:4000,used_bytes:100,
    free_bytes:3700,admissible_bytes:1}
];
"""


def test_summary_weights_real_physical_usage_and_lists_free_space_on_every_disk():
    actual = run('status.html', HARNESS, """
render({pools:original,host:{state:'ok'}});
console.log(JSON.stringify({
  percent:elements['storage-total-percent'].textContent,
  used:elements['storage-total-used'].textContent,
  total:elements['storage-total-bytes'].textContent,
  free:elements['storage-total-free'].textContent,
  bar:elements['storage-total-meter'].firstElementChild.style.width,
  aria:elements['storage-total-meter'].attributes,
  disks:original.map(pool=>elements[`storage-${pool.pool_id}-free`].textContent),
  label:elements['storage-hdd3-label'].textContent,
  hidden:elements['storage-overview'].hidden,
  closed:!elements['storage-more'].open,
}));
""")
    assert actual['percent'] == '30%'
    assert actual['bar'] == '30%'
    assert actual['used'] == '2,9 KiB'
    assert actual['total'] == '9,8 KiB'
    assert actual['free'] == '6,3 KiB'
    assert actual['aria']['aria-valuenow'] == '30'
    assert actual['disks'] == ['80 B', '900 B', '1,8 KiB', '3,6 KiB']
    assert actual['label'] == 'HD 18 TB'
    assert actual['closed'] is True
    assert actual['hidden'] is False


@pytest.mark.parametrize('invalid', [
    "{state:'unavailable'}", "{state:'stale'}", "{used_bytes:null}",
    "{used_bytes:3500,free_bytes:900}",
])
def test_one_invalid_disk_never_produces_a_partial_total_or_stale_free_space(invalid):
    actual = run('status.html', HARNESS, f"""
render({{pools:original,host:{{state:'ok'}}}});
render({{pools:original.map(pool=>pool.pool_id==='hdd3'?{{...pool,...{invalid}}}:pool)}});
console.log(JSON.stringify({{
  percent:elements['storage-total-percent'].textContent,
  total:elements['storage-total-bytes'].textContent,
  free:elements['storage-total-free'].textContent,
  aria:elements['storage-total-meter'].attributes,
  missing:elements['storage-hdd3-free'].textContent,
  other:elements['storage-hdd-free'].textContent,
  state:elements['storage-summary-note'].textContent,
}}));
""")
    assert actual['percent'] == actual['total'] == actual['free'] == '—'
    assert 'aria-valuenow' not in actual['aria']
    assert actual['missing'] == '—'
    assert actual['other'] == '900 B'
    assert '1 disco sem medição válida' in actual['state']


def test_polling_keeps_details_choice_and_network_errors_invalidate_summary():
    actual = run('status.html', HARNESS, """
(async()=>{
  render({pools:original,host:{state:'ok'}});
  elements['storage-more'].open=true;
  render({pools:original,host:{state:'ok'}});
  const keptOpen=elements['storage-more'].open;
  elements['storage-more'].open=false;
  render({pools:original,host:{state:'ok'}});
  const keptClosed=!elements['storage-more'].open;
  global.fetch=()=>Promise.reject(Error('fixture network failure'));
  await refresh();
  console.log(JSON.stringify({keptOpen,keptClosed,
    percent:elements['storage-total-percent'].textContent,
    diskFree:original.map(pool=>elements[`storage-${pool.pool_id}-free`].textContent),
    hidden:elements['storage-hdd3'].hidden}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
    assert actual['keptOpen'] is actual['keptClosed'] is True
    assert actual['percent'] == '—'
    assert actual['diskFree'] == ['—'] * 4
    assert actual['hidden'] is False


def test_old_two_disk_configuration_hides_unregistered_disks_and_legacy_has_no_empty_summary():
    actual = run('status.html', HARNESS, """
render({pools:original.slice(0,2)});
const actual={second:elements['storage-hdd2'].hidden,third:elements['storage-hdd3'].hidden,
  total:elements['storage-total-bytes'].textContent};
render({capacity:{total_bytes:1000,used_bytes:300,free_bytes:600}});
actual.summaryHidden=elements['storage-overview'].hidden;
actual.detailsHidden=elements['storage-more'].hidden;
actual.legacyVisible=!elements['legacy-storage'].hidden;
console.log(JSON.stringify(actual));
""")
    assert actual['second'] is actual['third'] is True
    assert actual['total'] == '2,9 KiB'
    assert actual['summaryHidden'] is actual['detailsHidden'] is actual['legacyVisible'] is True


class StorageMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.in_storage_details = False
        self.contained_cards = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == 'details' and attrs.get('id') == 'storage-more':
            self.in_storage_details = True
        if 'pool-card' in attrs.get('class', '').split():
            self.contained_cards.append((attrs['id'], self.in_storage_details))

    def handle_endtag(self, tag):
        if tag == 'details':
            self.in_storage_details = False


def test_pool_cards_are_inside_a_native_closed_details_control():
    markup = StorageMarkup()
    html = Path('services/telemetry/src/homeserver_telemetry/templates/status.html').read_text()
    markup.feed(html)
    detail = next(attrs for tag, attrs in markup.tags if attrs.get('id') == 'storage-more')
    assert 'open' not in detail
    assert ('details', detail) in markup.tags
    assert '<summary' in html.split('id="storage-more"', 1)[1].split('id="pool-storage"', 1)[0]
    assert 'Mais detalhes' in html
    assert html.index('id="storage-overview"') < html.index('id="storage-more"')
    assert markup.contained_cards == [
        (f'pool-{pool}-card', True) for pool in ['ssd', 'hdd', 'hdd2', 'hdd3']
    ]
