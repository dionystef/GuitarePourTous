'use strict';
/* Tests MISSION-06 (frontend) : onglets de dossiers & filtrage namespacé.
 *
 * Charge `static/app.js` dans un harnais Node via `vm` et un DOM simulé
 * (aucun navigateur). On vérifie :
 *   * `folderTabHTML` : les onglets système (`__all__` / `__none__`) ne sont
 *     PAS supprimables, tandis qu'un dossier utilisateur homonyme (« all »,
 *     « unclassified ») l'est — aucune collision possible ;
 *   * `filterTracksByFolder` : le filtre ne dépend jamais d'une égalité avec
 *     une chaîne utilisateur (les clés système sont préfixées) ;
 *   * `refreshLibrary` : l'onglet « Non classés » n'est ni masqué ni rendu
 *     supprimable par la présence d'un dossier utilisateur homonyme.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const CODE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'app.js'), 'utf8');

/* --------------------------- DOM simulé minimal -------------------------- */
function makeEl(tag = 'div') {
  const t = {
    id: '', tagName: tag.toUpperCase(), value: '', files: [], textContent: '',
    innerHTML: '', className: '', title: '', dataset: {}, children: [],
    scrollTop: 0, scrollHeight: 0, offsetHeight: 10,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; }, replace() {} },
    style: { setProperty() {}, getPropertyValue() { return ''; }, width: '', height: '' },
    _l: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, f) { (this._l[ev] = this._l[ev] || []).push(f); },
    removeEventListener(ev, f) { const a = this._l[ev]; if (a) { const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getAttribute() { return null; }, setAttribute() {}, removeAttribute() {},
    click() {}, remove() {}, closest() { return null; }, contains() { return false; }, focus() {},
  };
  return new Proxy(t, {
    get(o, p) { if (p in o) return o[p]; if (typeof p === 'string') return (o[p] = function () {}); return undefined; },
    set(o, p, v) { o[p] = v; return true; },
  });
}

function buildHarness(fetchFn) {
  const els = new Map();
  const store = {};
  const localStorage = {
    getItem(k) { return (k in store) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
  };
  const doc = (sel) => {
    if (!els.has(sel)) els.set(sel, makeEl(sel.startsWith('#') ? sel.slice(1) : 'div'));
    return els.get(sel);
  };
  let savedInit = null;
  const winListeners = new Map();
  const window = {
    addEventListener(ev, f) {
      if (ev === 'DOMContentLoaded') savedInit = f;
      const a = winListeners.get(ev) || []; a.push(f); winListeners.set(ev, a);
    },
    location: { protocol: 'http:', host: 'testserver' },
    App: {},
  };
  const document = {
    querySelector: doc,
    querySelectorAll() { return []; },
    createElement: makeEl,
    documentElement: makeEl('html'),
    body: makeEl('body'),
    addEventListener() {},
  };
  const navigator = { serviceWorker: { getRegistrations: async () => [] } };
  const caches = { keys: async () => [], delete: async () => true };
  let pollCb = null;
  const wsInstances = [];

  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this.onopen = null; this.onmessage = null; this.onerror = null; this.onclose = null; wsInstances.push(this); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
    __emit(data) { if (this.onmessage) this.onmessage({ data }); }
  }

  const sandbox = {
    window, document, navigator, caches, localStorage,
    console,
    setTimeout(fn) { fn(); return 0; },
    clearTimeout() {},
    setInterval(fn) { pollCb = fn; return 1; },
    clearInterval() { pollCb = null; },
    fetch: fetchFn,
    WebSocket: FakeWS,
    FormData: class { append() {} },
    alert() {}, confirm() { return true; }, prompt() { return null; },
    requestAnimationFrame() {},
    location: { protocol: 'http:', host: 'testserver' },
    URL, TextEncoder, TextDecoder, Blob,
  };
  sandbox.window.window = window;
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'app.js' });
  return { sandbox, doc, els, store, localStorage, savedInit, wsInstances };
}

/* Stub fetch : aiguillage par motif ; réponses JSON prédéfinies. */
function routeFetch(routes) {
  return async (url) => {
    for (const [pattern, val] of routes) {
      if (url.includes(pattern)) {
        if (val instanceof Error) throw val;
        return { ok: true, status: 200, json: async () => val, text: async () => '' };
      }
    }
    if (url.includes('/api/tracks')) {
      return { ok: true, status: 200, json: async () => [], text: async () => '' };
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
  };
}

const drain = () => new Promise((r) => setImmediate(r));

/* isole le HTML d'un onglet (entre son <button> et son </button>). */
function tabHTMLFor(html, folder) {
  const start = html.indexOf(`data-folder="${folder}"`);
  if (start === -1) return null;
  const open = html.lastIndexOf('<button', start);
  const close = html.indexOf('</button>', start);
  return html.slice(open, close + '</button>'.length);
}

/* ================================ Tests ================================== */

test('folderTabHTML : onglets système non supprimables', () => {
  const h = buildHarness(routeFetch([]));
  const allSys = h.sandbox.folderTabHTML('__all__', 'Tous', 5);
  const noneSys = h.sandbox.folderTabHTML('__none__', 'Non classés', 2);
  assert.ok(!allSys.includes('folder-tab-del'), 'Tous ne doit pas être supprimable');
  assert.ok(!noneSys.includes('folder-tab-del'), 'Non classés ne doit pas être supprimable');
});

test('folderTabHTML : dossier utilisateur homonyme supprimable', () => {
  const h = buildHarness(routeFetch([]));
  const allUser = h.sandbox.folderTabHTML('all', 'all', 1);
  const noneUser = h.sandbox.folderTabHTML('unclassified', 'unclassified', 1);
  assert.ok(allUser.includes('folder-tab-del'), 'dossier « all » utilisateur supprimable');
  assert.ok(noneUser.includes('folder-tab-del'), 'dossier « unclassified » utilisateur supprimable');
});

test('filterTracksByFolder : clés système namespacées', () => {
  const h = buildHarness(routeFetch([]));
  const tracks = [
    { id: 'a', folder: 'unclassified' },
    { id: 'b', folder: 'all' },
    { id: 'c', folder: '' },
  ];
  const f = (folder) => h.sandbox.filterTracksByFolder(tracks, folder).length;
  assert.strictEqual(f('__all__'), 3, 'Tous = tous les morceaux');
  assert.strictEqual(f('__none__'), 1, 'Non classés = uniquement les non classés');
  assert.strictEqual(f('unclassified'), 1, 'dossier utilisateur homonyme distinct');
  assert.strictEqual(f('all'), 1, 'dossier utilisateur « all » distinct');
});

test('refreshLibrary : l’onglet « Non classés » n’est ni masqué ni supprimable par un homonyme', async () => {
  // Des morceaux non classés ET un dossier utilisateur nommé « unclassified ».
  const tracks = [
    { id: 't1', folder: 'unclassified', title: 'Dans le dossier', status: 'ready', stems: [], artist: '' },
    { id: 't2', folder: null, title: 'Non classé', status: 'ready', stems: [], artist: '' },
  ];
  const h = buildHarness(routeFetch([
    ['/api/tracks', tracks],
    ['/api/folders', ['unclassified']],
  ]));
  await h.sandbox.refreshLibrary();
  await drain();
  const html = h.doc('#library-folders').innerHTML;

  // L'onglet système « Non classés » est bien présent.
  const noneSys = tabHTMLFor(html, '__none__');
  assert.ok(noneSys, 'l\'onglet système « Non classés » doit être rendu');
  assert.ok(noneSys.includes('Non classés'), 'libellé « Non classés » présent');
  assert.ok(!noneSys.includes('folder-tab-del'), 'onglet système Non classés non supprimable');

  // Le dossier utilisateur homonyme est un onglet séparé et supprimable.
  const noneUser = tabHTMLFor(html, 'unclassified');
  assert.ok(noneUser, 'le dossier utilisateur « unclassified » doit être rendu');
  assert.ok(noneUser.includes('folder-tab-del'), 'dossier utilisateur homonyme supprimable');
});
