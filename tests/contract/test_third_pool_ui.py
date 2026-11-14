"""Execute the browser render functions with three independent storage pools."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def run(template, harness, exercise):
    node = shutil.which('node') or shutil.which('node.exe')
    if not node:
        pytest.skip('Node required for browser rendering contract')
    html = Path('services/telemetry/src/homeserver_telemetry/templates', template).read_text()
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]
    program = (harness.replace('HTML', json.dumps(html))
               + '\nglobal.fetch=()=>new Promise(()=>{}); global.setInterval=()=>{};\n'
               + script + '\n' + exercise)
    result = subprocess.run([node], input=program, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_optional_second_hd_card_has_own_label_chart_and_no_legacy_ghost():
    actual = run('status.html', """
const elements = Object.fromEntries([...HTML.matchAll(/id="([^"]+)"/g)].map(m => [m[1], {
  textContent:'', hidden:false, dataset:{}, style:{setProperty(){}},
  firstElementChild:{style:{}}, attributes:{},
  setAttribute(key,value){this.attributes[key]=value;},
  removeAttribute(key){delete this.attributes[key];}
}]));
global.document={getElementById:id=>{if(!elements[id]) throw Error(id); return elements[id];}};
""", """
const pool={state:'ready',total_bytes:1000,used_bytes:600,free_bytes:350};
renderPools([{...pool,pool_id:'ssd'},{...pool,pool_id:'hdd'},
             {...pool,pool_id:'hdd2',label:'HD USB 2'}]);
const actual={visible:!elements['pool-hdd2-card'].hidden,
  title:elements['pool-hdd2-title'].textContent,
  label:elements['pool-hdd2-chart'].attributes['aria-label'],
  freeBar:elements['pool-hdd2-free-bar'].style.width};
renderPools([{...pool,pool_id:'ssd'},{...pool,pool_id:'hdd'}]);
actual.legacyHidden=elements['pool-hdd2-card'].hidden;
console.log(JSON.stringify(actual));
""")
    assert actual['visible'] is True
    assert actual['title'] == 'HD USB 2'
    assert actual['label'].startswith('HD USB 2:')
    assert 'Livre 35%' in actual['label']
    assert actual['freeBar'] == '35%'
    assert actual['legacyHidden'] is True


def test_download_on_second_hd_does_not_display_ssd():
    actual = run('downloads.html', """
const nodes=[];
function node(){return {children:[],style:{},append(...items){this.children.push(...items);},
  replaceChildren(){this.children=[];},setAttribute(){},addEventListener(){}};}
const roots={};
global.document={getElementById:id=>roots[id]||(roots[id]=node()),
  createElement:()=>{const result=node();nodes.push(result);return result;}};
""", """
render([{name:'fixture',infohash:'a',pool_id:'hdd2',progress:0.5,download_bps:20,
  remaining_bytes:50,seeds:2,state:'downloading',replace_allowed:true}]);
console.log(JSON.stringify(nodes.map(item=>item.textContent).filter(Boolean)));
""")
    assert 'HD USB 2' in actual
    assert 'SSD' not in actual


def test_refresh_failure_preserves_known_second_hd_without_stale_numbers():
    actual = run('status.html', """
const elements = Object.fromEntries([...HTML.matchAll(/id="([^"]+)"/g)].map(m => [m[1], {
  textContent:'', hidden:false, dataset:{}, style:{setProperty(){}},
  firstElementChild:{style:{}}, attributes:{},
  setAttribute(key,value){this.attributes[key]=value;},
  removeAttribute(key){delete this.attributes[key];}
}]));
global.document={getElementById:id=>{if(!elements[id]) throw Error(id); return elements[id];}};
""", """
(async()=>{
  const pool={state:'ready',total_bytes:1000,used_bytes:600,free_bytes:350,
    active_queue_remaining_bytes:20,admissible_bytes:300};
  const original=[{...pool,pool_id:'ssd'},{...pool,pool_id:'hdd'},
    {...pool,pool_id:'hdd2',label:'HD de mídia 2'}];
  render({pools:original,host:{state:'ok'}});
  global.fetch=()=>Promise.reject(Error('transient fixture failure'));
  await refresh();
  const actual={visible:!elements['pool-hdd2-card'].hidden,
    title:elements['pool-hdd2-title'].textContent,
    state:elements['pool-hdd2-state'].dataset.state,
    free:elements['pool-hdd2-free'].textContent,
    queue:elements['pool-hdd2-queue'].textContent,
    admissible:elements['pool-hdd2-admissible'].textContent,
    age:elements['pool-hdd2-age'].textContent,
    graphHidden:elements['pool-hdd2-chart'].hidden,
    graphLabel:elements['pool-hdd2-chart'].attributes['aria-label'],
    freeBar:elements['pool-hdd2-free-bar'].style.width};
  render({pools:original.slice(0,2),host:{state:'ok'}});
  actual.hiddenAfterValidOmission=elements['pool-hdd2-card'].hidden;
  await refresh();
  actual.stillHidden=elements['pool-hdd2-card'].hidden;
  console.log(JSON.stringify(actual));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
    assert actual['visible'] is True
    assert actual['title'] == 'HD de mídia 2'
    assert actual['state'] == 'unavailable'
    assert actual['free'] == actual['queue'] == actual['admissible'] == actual['age'] == '—'
    assert actual['graphHidden'] is True
    assert actual['graphLabel'] == 'HD de mídia 2: gráfico indisponível'
    assert actual['freeBar'] == '0%'
    assert actual['hiddenAfterValidOmission'] is actual['stillHidden'] is True
