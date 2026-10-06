'use strict';
/* Tests MISSION-01 (frontend) : reprise automatique d'un décorticage.
 *
 * Charge `static/app.js` dans un harnais Node sans framework (module `vm` +
 * objet DOM simulé par Proxy) : aucun navigateur, aucune dépendance externe.
 * On vérifie la persistance localStorage de `guitarlab_active_job` :
 *   * mémorisation au lancement de `monitorJob(id)` ;
 *   * nettoyage dans `finish()` sur terminaison (ready/error) ;
 *   * reprise dans `init()` via `API.activeJobs()` (sinon repli localStorage) ;
 *   * `autoOpen=false` (pas d'ouverture du lecteur à la reprise) ;
 *   * comportement lorsqu'un id mémorisé devient invalide (404).
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

  // WebSocket pilotable : chaque instance expose onopen/onmessage/onerror/onclose
  // et peut être pilotée par le test pour simuler le flux de statut.
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this.onopen = null; this.onmessage = null; this.onerror = null; this.onclose = null; wsInstances.push(this); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
    __emit(data) { if (this.onmessage) this.onmessage({ data }); }
  }

  const sandbox = {
    window, document, navigator, caches, localStorage,
    console,
    // Timers neutralisés : pas de vrai timer → le process Node se termine.
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

/* Stub fetch : aiguillage simple par motif dans l'URL. Retourne le JSON fourni,
   ou lève une erreur (réseau/HTTP) si la valeur est un `Error` ou 404. */
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
    // Par défaut : la liste des morceaux est vide (evite `tracks.map is not a function`
    // dans `refreshLibrary`, appelée par `finish`).
    if (url.includes('/api/tracks')) {
      return { ok: true, status: 200, json: async () => [], text: async () => '' };
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
  };
}

// Drainage des microtâches/promesses non attendues (refreshLibrary, etc.).
const drain = () => new Promise((r) => setImmediate(r));

/* ------------------------------ Témoins spies ---------------------------- */
function spyMap(sandbox) {
  const calls = { monitorJob: [], openPlayer: [] };
  const origMonitor = sandbox.monitorJob;
  const origOpen = sandbox.openPlayer;
  sandbox.monitorJob = (id, autoOpen) => {
    calls.monitorJob.push({ id, autoOpen });
    return origMonitor(id, autoOpen);
  };
  sandbox.openPlayer = (id) => { calls.openPlayer.push(id); return Promise.resolve(); };
  return calls;
}

/* ================================ Tests ================================== */
test('monitorJob mémorise guitarlab_active_job', () => {
  const h = buildHarness(routeFetch([]));
  h.sandbox.monitorJob('trackA');
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), 'trackA');
  h.flushPoll; // (pas d'interval déclenché)
});

test('finish(ready) nettoie guitarlab_active_job', async () => {
  const h = buildHarness(routeFetch([
    ['/api/status/trackA', { status: 'ready', progress: 100, logs: ['ok'] }],
  ]));
  h.sandbox.monitorJob('trackA'); // autoOpen = true par défaut
  await h.flushPoll();
  await drain();
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), null);
});

test('finish(error) nettoie guitarlab_active_job', async () => {
  const h = buildHarness(routeFetch([
    ['/api/status/trackB', { status: 'error', progress: 100, logs: ['boom'] }],
  ]));
  h.sandbox.monitorJob('trackB');
  await h.flushPoll();
  await drain();
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), null);
});

test('init reprend actif[0] depuis activeJobs avec autoOpen=false', async () => {
  const h = buildHarness(routeFetch([
    ['/api/jobs/active', [{ id: 'trackA' }]],
    ['/api/health', { device: 'cpu', version: '1' }],
    ['/api/tracks', []],
  ]));
  const c = spyMap(h.sandbox);
  await h.sandbox.init();
  assert.strictEqual(c.monitorJob.length, 1);
  assert.strictEqual(c.monitorJob[0].id, 'trackA');
  assert.strictEqual(c.monitorJob[0].autoOpen, false);
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), 'trackA');
});

test('init bascule sur localStorage si le backend est hors-ligne', async () => {
  const h = buildHarness(routeFetch([
    ['/api/jobs/active', new Error('ECONNREFUSED')],
    ['/api/health', new Error('offline')],
  ]));
  h.localStorage.setItem('guitarlab_active_job', 'savedId');
  const c = spyMap(h.sandbox);
  await h.sandbox.init();
  assert.strictEqual(c.monitorJob.length, 1);
  assert.strictEqual(c.monitorJob[0].id, 'savedId');
  assert.strictEqual(c.monitorJob[0].autoOpen, false);
});

test('init ne reprend rien quand activeJobs vide et aucun id mémorisé', async () => {
  const h = buildHarness(routeFetch([
    ['/api/jobs/active', []],
    ['/api/health', { device: 'cpu', version: '1' }],
    ['/api/tracks', []],
  ]));
  const c = spyMap(h.sandbox);
  await h.sandbox.init();
  assert.strictEqual(c.monitorJob.length, 0);
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), null);
});

test('reprise avec plusieurs jobs actifs : reprend le premier', async () => {
  const h = buildHarness(routeFetch([
    ['/api/jobs/active', [{ id: 'jA' }, { id: 'jB' }]],
    ['/api/health', { device: 'cpu', version: '1' }],
    ['/api/tracks', []],
  ]));
  const c = spyMap(h.sandbox);
  await h.sandbox.init();
  assert.strictEqual(c.monitorJob[0].id, 'jA');
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), 'jA');
});

/* MISSION-01 (CORRIGÉ) : un id mémorisé devenu invalide (404) doit nettoyer
   `guitarlab_active_job` et arrêter le suivi. `API.json` porte désormais
   `e.status = r.status`, ce qui permet au polling de `monitorJob` de détecter
   la 404 et d'appeler `finish('error')` (suppression de la clé + arrêt). */
test('MISSION-01 : id mémorisé invalide (404) nettoie localStorage et stoppe le suivi', async () => {
  let statusCalls = 0;
  const base = routeFetch([
    ['/api/status/ghostId', 404],
  ]);
  const h = buildHarness(async (url) => {
    if (url.includes('/api/status/')) statusCalls += 1;
    return base(url);
  });
  h.localStorage.setItem('guitarlab_active_job', 'ghostId');
  h.sandbox.monitorJob('ghostId');
  await h.flushPoll(); // le polling détecte la 404 → finish('error')
  await drain();
  // Après correctif : la clé est supprimée (plus de fuite localStorage).
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), null);
  // …et le suivi s'arrête : un tick ultérieur ne déclenche plus de requête.
  const callsAtStop = statusCalls;
  await h.flushPoll();
  assert.strictEqual(statusCalls, callsAtStop);
});

test('erreur réseau transitoire (non-404) : silencieuse, la clé persiste et le polling continue', async () => {
  let statusCalls = 0;
  const h = buildHarness(async (url) => {
    if (url.includes('/api/status/')) statusCalls += 1;
    throw new Error('ECONNREFUSED'); // backend pas encore joignable
  });
  h.localStorage.setItem('guitarlab_active_job', 'jobY');
  h.sandbox.monitorJob('jobY');
  await h.flushPoll();
  await drain();
  // Une erreur réseau ne doit PAS être traitée comme un 404 : la clé reste.
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), 'jobY');
  assert.strictEqual(statusCalls, 1, 'le polling continue de réessayer');
  // Un tick suivant réessaie encore (résilience, pas d’arrêt définitif).
  await h.flushPoll();
  assert.strictEqual(statusCalls, 2);
});

test('erreur serveur 500 : silencieuse, la clé persiste (seul un 404 stoppe)', async () => {
  const h = buildHarness(routeFetch([
    ['/api/status/jobZ', 500],
  ]));
  h.localStorage.setItem('guitarlab_active_job', 'jobZ');
  h.sandbox.monitorJob('jobZ');
  await h.flushPoll();
  await drain();
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), 'jobZ');
});

test('WS actif : le polling est suspendu (usingWs), et la clé n’est pas nettoyée par le polling', async () => {
  const h = buildHarness(routeFetch([
    ['/api/status/wsJob', 404],
  ]));
  h.localStorage.setItem('guitarlab_active_job', 'wsJob');
  h.sandbox.monitorJob('wsJob');
  assert.strictEqual(h.wsInstances.length, 1);
  // On ouvre la socket : usingWs = true → le polling est court-circuité.
  h.wsInstances[0].onopen();
  const before = h.localStorage.getItem('guitarlab_active_job');
  await h.flushPoll(); // aucun appel API.status (usingWs)
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), before);
});

test('WS onmessage ready → finish nettoie la clé (cohérence WS/polling)', async () => {
  const h = buildHarness(routeFetch([
    ['/api/status/wsEnd', { status: 'ready', progress: 100, logs: [] }],
  ]));
  h.localStorage.setItem('guitarlab_active_job', 'wsEnd');
  h.sandbox.monitorJob('wsEnd');
  const ws = h.wsInstances[0];
  ws.onopen();
  // Le statut terminal arrive via la socket : finish est appelé, clé nettoyée.
  ws.__emit(JSON.stringify({ type: 'status', status: { status: 'ready', progress: 100, logs: [] } }));
  await drain();
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), null);
});

test('WS fermée (onclose) → polling reprend et un 404 nettoie la clé', async () => {
  const h = buildHarness(routeFetch([
    ['/api/status/wsClosed', 404],
  ]));
  h.localStorage.setItem('guitarlab_active_job', 'wsClosed');
  h.sandbox.monitorJob('wsClosed');
  const ws = h.wsInstances[0];
  ws.onopen(); // usingWs = true → polling suspendu
  await h.flushPoll();
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), 'wsClosed');
  // Le serveur ferme la socket (morceau supprimé → close 1008) : usingWs = false.
  ws.close();
  // Le polling reprend et détecte la 404 → finish('error') → clé nettoyée.
  await h.flushPoll();
  await drain();
  assert.strictEqual(h.localStorage.getItem('guitarlab_active_job'), null);
});
