import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('./local/abyss-defaults.js', import.meta.url), 'utf8');
const marker = 'homeserver.abyss.legacy-layout-v1';
function boot(values = {}, options = {}) {
  const storage = new Map(Object.entries(values));
  const events = new Map();
  vm.runInNewContext(source, {
    localStorage: {
      getItem(key) { if (options.denied) throw Error('blocked'); return storage.get(key) ?? null; },
      setItem(key, value) {
        if (options.denied || options.failWrite === key) throw Error('blocked');
        storage.set(key, value);
      },
    },
    navigator: { userAgent: options.agent || 'Windows Chrome' },
    window: { addEventListener(name, fn) { events.set(name, fn); } },
  });
  return { storage, events };
}
assert.equal(boot().storage.get('layout'), 'desktop-legacy');
assert.equal(boot({}, {agent:'Android Mobile'}).storage.get('layout'), 'mobile-legacy');
for (const agent of ['Windows Chrome', 'Android Mobile', 'iPhone']) {
  for (const layout of ['', 'auto', 'modern', 'desktop', 'mobile']) {
    const expected = layout === 'mobile' ? 'mobile-legacy'
      : layout === 'desktop' ? 'desktop-legacy'
      : agent === 'Windows Chrome' ? 'desktop-legacy' : 'mobile-legacy';
    const result = boot({layout}, {agent});
    assert.equal(result.storage.get('layout'), expected, `migrate existing ${layout} on ${agent}`);
    assert.equal(result.storage.get(marker), '1');
  }
}
for (const layout of ['tv', 'desktop-legacy', 'mobile-legacy', 'future-unknown-mode']) {
  assert.equal(boot({layout}).storage.get('layout'), layout, 'Legacy, TV and unknown modes preserved');
}
for (const agent of ['SmartTV', 'Tizen', 'Web0S', 'Android TV', 'GoogleTV', 'Fire TV', 'AFTMM']) {
  for (const values of [{}, {layout:'modern'}, {layout:'auto'}]) {
    assert.equal(boot(values, {agent}).storage.get('layout'), values.layout, 'TV device preserved');
  }
}
const migrated = boot({layout:'modern'});
assert.equal(boot(Object.fromEntries(migrated.storage)).storage.get('layout'), 'desktop-legacy');
for (const layout of ['', 'auto', 'modern', 'desktop', 'mobile', 'tv']) {
  assert.equal(boot({layout,[marker]:'1'}).storage.get('layout'), layout, 'subsequent choice preserved');
}
assert.equal(boot({[marker]:'1'}).storage.get('layout'), undefined, 'no forced defaults after migration');
const failedLayout = boot({layout:'modern'}, {failWrite:'layout'});
assert.equal(failedLayout.storage.get('layout'), 'modern');
assert.equal(failedLayout.storage.has(marker), false, 'failed write must not mark migration complete');
assert.equal(boot(Object.fromEntries(failedLayout.storage)).storage.get('layout'), 'desktop-legacy');
const failedMarker = boot({layout:'modern'}, {failWrite:marker});
assert.equal(failedMarker.storage.get('layout'), 'desktop-legacy');
assert.equal(failedMarker.storage.has(marker), false);
assert.equal(boot(Object.fromEntries(failedMarker.storage)).storage.get(marker), '1', 'partial migration retries');
const userId = 'a'.repeat(32);
const credentials = JSON.stringify({Servers:[{UserId:userId, AccessToken:'fixture-only'}]});
const initial = boot({jellyfin_credentials:credentials});
assert.equal(initial.storage.get(`${userId}-enableBackdrops`), 'true');
assert.equal(initial.storage.get('jellyfin_credentials'), credentials, 'session remains unchanged');
assert.equal(boot({jellyfin_credentials:credentials,[`${userId}-enableBackdrops`]:'false'})
  .storage.get(`${userId}-enableBackdrops`), 'false', 'explicit backdrop preference preserved');
const fresh = boot();
fresh.storage.set('jellyfin_credentials', credentials);
fresh.events.get('hashchange')();
assert.equal(fresh.storage.get(`${userId}-enableBackdrops`), 'true', 'first login gets default');
assert.doesNotThrow(() => boot({jellyfin_credentials:'invalid'}));
assert.doesNotThrow(() => boot({}, {denied:true}));
console.log('Abyss defaults: existing-browser migration, later choices, TVs, storage failures and first login passed');
