"""The optional fourth pool remains visible independently of the earlier disks."""
from test_third_pool_ui import run

STATUS_HARNESS = """
const elements = Object.fromEntries([...HTML.matchAll(/id="([^"]+)"/g)].map(m => [m[1], {
  textContent:'', hidden:false, dataset:{}, style:{setProperty(){}},
  firstElementChild:{style:{}}, attributes:{},
  setAttribute(key,value){this.attributes[key]=value;},
  removeAttribute(key){delete this.attributes[key];}
}]));
global.document={getElementById:id=>{if(!elements[id]) throw Error(id); return elements[id];}};
"""


def test_fourth_pool_card_hides_when_unregistered_and_retains_identity_on_network_failure():
    actual = run('status.html', STATUS_HARNESS, """
(async()=>{
  const pool={state:'ready',total_bytes:1000,used_bytes:600,free_bytes:350,
    active_queue_remaining_bytes:20,admissible_bytes:300};
  const original=['ssd','hdd','hdd2','hdd3'].map(id=>({...pool,pool_id:id}));
  render({pools:original,host:{state:'ok'}});
  const actual={visible:!elements['pool-hdd3-card'].hidden,
    title:elements['pool-hdd3-title'].textContent,
    chart:elements['pool-hdd3-chart'].attributes['aria-label'],
    freeBar:elements['pool-hdd3-free-bar'].style.width};
  global.fetch=()=>Promise.reject(Error('transient fixture failure'));
  await refresh();
  actual.failedVisible=!elements['pool-hdd3-card'].hidden;
  actual.failedState=elements['pool-hdd3-state'].dataset.state;
  actual.failedFree=elements['pool-hdd3-free'].textContent;
  render({pools:original.slice(0,3),host:{state:'ok'}});
  actual.hiddenAfterOmission=elements['pool-hdd3-card'].hidden;
  actual.thirdVisible=!elements['pool-hdd2-card'].hidden;
  console.log(JSON.stringify(actual));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
    assert actual['visible'] is actual['failedVisible'] is actual['thirdVisible'] is True
    assert actual['title'] == 'HD USB 3'
    assert actual['chart'].startswith('HD USB 3:')
    assert actual['freeBar'] == '35%'
    assert actual['failedState'] == 'unavailable'
    assert actual['failedFree'] == '—'
    assert actual['hiddenAfterOmission'] is True


def test_fourth_pool_download_displays_its_own_disk_label():
    actual = run('downloads.html', """
const nodes=[];
function node(){return {children:[],style:{},append(...items){this.children.push(...items);},
  replaceChildren(){this.children=[];},setAttribute(){},addEventListener(){}};}
const roots={};
global.document={getElementById:id=>roots[id]||(roots[id]=node()),
  createElement:()=>{const result=node();nodes.push(result);return result;}};
""", """
render([{name:'fixture',infohash:'a',pool_id:'hdd3',progress:0.5,download_bps:20,
  remaining_bytes:50,seeds:2,state:'downloading',replace_allowed:true}]);
console.log(JSON.stringify(nodes.map(item=>item.textContent).filter(Boolean)));
""")
    assert 'HD USB 3' in actual
    assert 'Disco indisponível' not in actual
