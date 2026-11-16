import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('./local/abyss-defaults.js', import.meta.url), 'utf8');
function boot(values = {}, mobile = false, denied = false) {
  const storage = new Map(Object.entries(values));
  const events = new Map();
  vm.runInNewContext(source, {
    localStorage: {
      getItem(key) { if (denied) throw Error('blocked'); return storage.get(key) ?? null; },
      setItem(key, value) { if (denied) throw Error('blocked'); storage.set(key, value); },
    },
    navigator: { userAgent: mobile ? 'Android Mobile' : 'Windows Chrome' },
    window: { addEventListener(name, fn) { events.set(name, fn); } },
  });
  return { storage, events };
}
assert.equal(boot().storage.get('layout'), 'desktop-legacy');
assert.equal(boot({}, true).storage.get('layout'), 'mobile-legacy');
for (const layout of ['', 'auto', 'modern', 'mobile', 'tv', 'desktop-legacy']) {
  assert.equal(boot({layout}).storage.get('layout'), layout, 'explicit device selection preserved');
}
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
assert.doesNotThrow(() => boot({}, false, true));
console.log('Abyss defaults: responsive Legacy, first login and existing choices passed');
