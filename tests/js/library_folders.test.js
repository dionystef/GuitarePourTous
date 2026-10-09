'use strict';
/* Tests MISSION-06 (frontend) : renommage titre/artiste + dossiers virtuels.
 *
 * Charge `static/app.js` dans un harnais Node sans framework (module `vm` +
 * objet DOM simulé par Proxy) : aucun navigateur, aucune dépendance externe.
 *
 * On couvre la logique pure et le rendu du filtre bibliothèque :
 *   * `filterTracksByFolder` : sélection 'all' / 'unclassified' / dossier nommé ;
 *   * `folderTabHTML` : onglet actif, croix de suppression réservée aux dossiers
 *     personnalisés, échappement HTML (anti-injection) ;
 *   * `renderFolderTabs` : onglets « Tous », « Non classés », dossiers custom et
 *     bouton « ＋ Dossier » ;
 *   * `renderCards` / `refreshLibrary` : grille filtrée + compteur de morceaux,
 *     tolérance au backend hors-ligne.
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
    alert() {}, confirm() { return true; },
    requestAnimationFrame() {},
    location: { protocol: 'http:', host: 'testserver' },
    URL, TextEncoder, TextDecoder, Blob,
  };
  sandbox.window.window = window;
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'app.js' });
  return {
    sandbox, doc, els, store, localStorage, savedInit, wsInstances,
    flushPoll: async () => { if (pollCb) await pollCb(); },
  };
}

/* Stub fetch : aiguillage par motif dans l'URL. */
function routeFetch(routes) {
  return async (url) => {
    for (const [pattern, val] of routes) {
      if (url.includes(pattern)) {
        if (val instanceof Error) throw val;
        if (typeof val === 'number' && (val === 404 || val === 500)) {
          return { ok: false, status: val, statusText: String(val), text: async () => '' };
        }
        return { ok: true, status: 200, json: async () => val, text: async () => '' };
      }
    }
    // Par défaut : la liste des morceaux est vide (évite `tracks.map is not a function`).
    if (url.includes('/api/tracks')) {
      return { ok: true, status: 200, json: async () => [], text: async () => '' };
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
  };
}

function track(id, folder, opts = {}) {
  return Object.assign({ id, title: `Titre ${id}`, artist: '', status: 'ready',
                         duration: null, bpm: null, thumbnail: null, stems: [], source: '' },
                       opts, { folder });
}

// Drainage des microtâches/promesses non attendues (refreshLibrary, etc.).
const drain = () => new Promise((r) => setImmediate(r));

/* ================================ Tests ================================== */

/* ------------------------- filterTracksByFolder --------------------------- */
// Les onglets système utilisent des clés préfixées (__all__ / __none__) : la
// logique de filtre ne dépend jamais d'une égalité avec une chaîne utilisateur.
test('filterTracksByFolder: la clé système "__all__" renvoie tous les morceaux', () => {
  const h = buildHarness(routeFetch([]));
  const tracks = [track('a', 'Rock'), track('b', ''), track('c', null)];
  assert.deepStrictEqual(h.sandbox.filterTracksByFolder(tracks, '__all__'), tracks);
});

test('filterTracksByFolder: "__none__" ne garde que les morceaux sans dossier', () => {
  const h = buildHarness(routeFetch([]));
  const tracks = [track('a', 'Rock'), track('b', ''), track('c', '   '), track('d', null)];
  const out = h.sandbox.filterTracksByFolder(tracks, '__none__');
  assert.deepStrictEqual(out.map(t => t.id), ['b', 'c', 'd']);
});

test('filterTracksByFolder: un dossier nommé filtre en normalisant les espaces', () => {
  const h = buildHarness(routeFetch([]));
  // Le filtre trime le folder avant comparaison : ' Rock ' doit matcher 'Rock'.
  const tracks = [track('a', 'Rock'), track('b', 'Blues'), track('c', ' Rock ')];
  assert.deepStrictEqual(h.sandbox.filterTracksByFolder(tracks, 'Rock').map(t => t.id), ['a', 'c']);
  assert.deepStrictEqual(h.sandbox.filterTracksByFolder(tracks, 'Blues').map(t => t.id), ['b']);
});

/* ----------------------------- folderTabHTML ------------------------------ */
test('folderTabHTML: l\'onglet "Tous" est actif par défaut', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.folderTabHTML('__all__', 'Tous', 3);
  assert.match(html, /folder-tab active/);
  assert.match(html, /data-folder="__all__"/);
  assert.match(html, /Tous/);
  assert.match(html, /folder-tab-count">3</);
});

test('folderTabHTML: un dossier personnalisé n\'est pas actif par défaut', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.folderTabHTML('Rock', 'Rock', 2);
  assert.doesNotMatch(html, /folder-tab active/);
  assert.match(html, /data-folder="Rock"/);
});

test('folderTabHTML: croix de suppression réservée aux dossiers personnalisés', () => {
  const h = buildHarness(routeFetch([]));
  assert.match(h.sandbox.folderTabHTML('Rock', 'Rock', 1), /folder-tab-del/);
  assert.doesNotMatch(h.sandbox.folderTabHTML('__all__', 'Tous', 1), /folder-tab-del/);
  assert.doesNotMatch(h.sandbox.folderTabHTML('__none__', 'Non classés', 1), /folder-tab-del/);
});

test('folderTabHTML: échappe les entrées (anti-injection HTML)', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.folderTabHTML('<img src=x>', '<b>Label</b>', 1);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /<b>Label<\/b>/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;b&gt;Label&lt;\/b&gt;/);
});

/* ---------------------------- renderFolderTabs ---------------------------- */
test('renderFolderTabs: construit Tous, Non classés, dossiers custom et +Dossier', async () => {
  const h = buildHarness(routeFetch([
    ['/api/tracks', [track('a', 'Rock'), track('b', ''), track('c', 'Jazz')]],
    ['/api/folders', ['Rock', 'Jazz', 'Blues']],
  ]));
  await h.sandbox.refreshLibrary();
  await drain();
  const bar = h.doc('#library-folders');
  assert.match(bar.innerHTML, /data-folder="__all__"/);
  assert.match(bar.innerHTML, /data-folder="__none__"/); // 1 non classé
  assert.match(bar.innerHTML, /data-folder="Rock"/);
  assert.match(bar.innerHTML, /data-folder="Jazz"/);
  assert.match(bar.innerHTML, /data-folder="Blues"/);
  assert.match(bar.innerHTML, /btn-add-folder/); // « ＋ Dossier »
});

test('renderFolderTabs: cache "Non classés" quand il n\'y en a aucun', async () => {
  const h = buildHarness(routeFetch([
    ['/api/tracks', [track('a', 'Rock'), track('b', 'Jazz')]],
    ['/api/folders', ['Rock', 'Jazz']],
  ]));
  await h.sandbox.refreshLibrary();
  await drain();
  assert.doesNotMatch(h.doc('#library-folders').innerHTML, /data-folder="__none__"/);
});

/* ---------------------------- renderCards / refreshLibrary ----------------- */
test('refreshLibrary: rend la grille filtrée avec compteur et carte épurée (T-UI-08)', async () => {
  const h = buildHarness(routeFetch([
    ['/api/tracks', [track('a', 'Rock', { title: 'Premier' }),
                     track('b', '', { title: 'Non classé' })]],
    ['/api/folders', ['Rock']],
  ]));
  await h.sandbox.refreshLibrary();
  await drain();
  const lib = h.doc('#library');
  assert.match(lib.innerHTML, /data-title="Premier"/);
  assert.match(lib.innerHTML, /data-folder="Rock"/);
  // Depuis la refonte T-UI-08, le sélecteur de dossier et le bouton « renommer »
  // ne sont plus sur la carte : ils sont déplacés dans le menu contextuel « ••• ».
  assert.doesNotMatch(lib.innerHTML, /<option value="Rock"/);
  assert.doesNotMatch(lib.innerHTML, /card-edit/);
  assert.doesNotMatch(lib.innerHTML, /folder-select/);
  assert.doesNotMatch(lib.innerHTML, /Ouvrir le labo/);
  // La nouvelle carte est entièrement cliquable + bouton d'actions « ••• ».
  assert.match(lib.innerHTML, /card-menu-btn/);
  assert.match(lib.innerHTML, /status-dot ready/);
  assert.strictEqual(h.doc('#lib-count').textContent, '2 morceaux');
});

test('refreshLibrary: tolère un backend hors-ligne (bibliothèque vide, pas d\'erreur)', async () => {
  const h = buildHarness(async () => { throw new Error('offline'); });
  await h.sandbox.refreshLibrary();
  await drain();
  assert.strictEqual(h.doc('#library').innerHTML, '');
  assert.strictEqual(h.doc('#lib-count').textContent, '0 morceau');
  // L'onglet « Tous » reste présent, sans onglet « Non classés ».
  assert.match(h.doc('#library-folders').innerHTML, /data-folder="__all__"/);
  assert.doesNotMatch(h.doc('#library-folders').innerHTML, /data-folder="__none__"/);
});
