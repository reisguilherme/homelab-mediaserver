// Execute the shipped loader's message handler; DOM rendering is verified in a browser.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const directory = process.argv[2] || fileURLToPath(new URL('./runtime/', import.meta.url));
const loader = await readFile(join(directory, 'spotlight-loader.js'), 'utf8');
const html = await readFile(join(directory, 'spotlight.html'), 'utf8');
new vm.Script(loader);
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1);
new vm.Script(scripts[0][1]);

const origin = 'https://streaming.example.test';
const listeners = new Map();
const requested = [];
const frameWindow = {};
const layer = () => ({ style: {}, classList: { add() {}, remove() {} } });
const context = vm.createContext({
  URL,
  console,
  testFrame: { contentWindow: frameWindow },
  testLayers: [layer(), layer()],
  Image: class {
    set src(value) { requested.push(value); this.onload?.(); }
  },
  requestAnimationFrame(fn) { fn(); },
  setTimeout() {},
  window: {
    location: { origin },
    addEventListener(type, callback) { listeners.set(type, callback); },
  },
  document: {
    readyState: 'loading',
    addEventListener() {},
    currentScript: { src: `${origin}/jellyfin/web/ui/spotlight-loader.js` },
    documentElement: { classList: { contains() { return true; } } },
  },
});
// Expose no production test API: initialize only the closure's existing DOM references here.
const seam = '\n  start();\n})();';
assert.equal(loader.split(seam).length, 2);
vm.runInContext(loader.replace(seam, `
  currentIframe = testFrame;
  backdropOverlay = {};
  backdropLayerA = testLayers[0];
  backdropLayerB = testLayers[1];
  start();
})();`), context);
const dispatch = (url, eventOrigin = origin, source = frameWindow) => listeners.get('message')({
  source, origin: eventOrigin,
  data: { type: 'abyss-spotlight', action: 'backdrop-sync', url },
});
const allowedBlob = `blob:${origin}/approved-image`;
dispatch(allowedBlob);
assert.deepEqual(requested, [allowedBlob], 'same-origin authenticated blob must render');
requested.length = 0;
dispatch(allowedBlob, 'https://untrusted.example');
dispatch(allowedBlob, origin, {});
for (const url of [
  `${origin}/Items/123/Images/Backdrop`,
  'https://untrusted.example/tracker.png',
  'blob:https://untrusted.example/image',
  `blob:${origin}.untrusted.example/image`,
  'blob:null/image',
  'data:image/svg+xml,<svg></svg>',
  'javascript:alert(1)',
  '',
  null,
]) dispatch(url);
assert.deepEqual(requested, [], 'untrusted messages and non-local blobs must not trigger requests');
console.log('Abyss runtime JS: parse and message/blob boundaries passed');
