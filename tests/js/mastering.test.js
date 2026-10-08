'use strict';
/* Tests MISSION-MASTER-UI : intégration du mastering et du comparateur A/B
 * dans le lecteur (static/app.js).
 *
 * Deux niveaux :
 *   1. Moteur audio (AudioEngine) — chargement de la piste masterisée,
 *      bascule A/B instantanée (gains, pas de rechargement), resynchronisation,
 *      réinitialisation.
 *   2. UI buildPlayer — déclenchement du mastering depuis les faders de stems
 *      (corps POST correct), révélation du comparateur A/B, téléchargement,
 *      gestion d'erreur, presets.
 *
 * Harnais Node sans navigateur (module `vm` + DOM simulé par Proxy), comme
 * `resume.test.js`. On fournit un AudioContext factice (decodeAudioData,
 * createGain, createBufferSource) et un fetch stub malgré le `arrayBuffer()`.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const CODE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'app.js'), 'utf8');

/* ----------------------------- DOM simulé -------------------------------- */
function makeClassList() {
  const set = new Set();
  return {
    add(...c) { c.forEach(x => set.add(x)); },
    remove(...c) { c.forEach(x => set.delete(x)); },
    toggle(c, force) {
      const has = set.has(c);
      const on = (force === undefined) ? !has : !!force;
      if (on) set.add(c); else set.delete(c);
      return on;
    },
    contains(c) { return set.has(c); },
    replace(a, b) { set.delete(a); set.add(b); },
    _set: set,
  };
}

function makeEl(tag = 'div') {
  const t = {
    id: '', tagName: tag.toUpperCase(), value: '', files: [], textContent: '',
    innerHTML: '', className: '', title: '', href: '', download: '', disabled: false,
    dataset: {}, children: [], scrollTop: 0, scrollHeight: 0, offsetHeight: 10,
    classList: makeClassList(),
    style: { setProperty() {}, getPropertyValue() { return ''; }, width: '', height: '' },
    _l: {}, _attrs: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, f) { (this._l[ev] = this._l[ev] || []).push(f); },
    removeEventListener(ev, f) { const a = this._l[ev]; if (a) { const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } },
    dispatch(ev) { (this._l[ev] || []).forEach(f => f({ target: this })); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getAttribute(n) { return (n in this._attrs) ? this._attrs[n] : null; },
    setAttribute(n, v) { this._attrs[n] = v; },
    removeAttribute() {}, click() {}, remove() {}, closest() { return null; },
    contains() { return false; }, focus() {},
  };
  return new Proxy(t, {
    get(o, p) { if (p in o) return o[p]; if (typeof p === 'string') return (o[p] = function () {}); return undefined; },
    set(o, p, v) { o[p] = v; return true; },
  });
}

/* --------------------------- AudioContext factice ------------------------ */
class FakeParam { constructor() { this.value = 0; } setTargetAtTime(v) { this.value = v; } }

class FakeNode {
  constructor(ctx) { this.ctx = ctx; this.gain = new FakeParam(); this.buffer = { duration: 10 }; }
  connect() {}
}

class FakeBufferSource {
  constructor(ctx) {
    this.ctx = ctx;
    this.playbackRate = { value: 1 };
    this.detune = { value: 0 };
    this.buffer = { duration: 10 };
    this.preservesPitch = true;
    this.onended = null;
    this.startedWith = null;
  }
  connect() {}
  start(t, off) { this.startedWith = { t, off }; }
  stop() {}
}

class FakeAudioContext {
  constructor() { this.currentTime = 0; this.state = 'running'; this.sampleRate = 44100; this.destination = {}; this.closed = false; }
  createGain() { return new FakeNode(this); }
  createBufferSource() { return new FakeBufferSource(this); }
  decodeAudioData() { return Promise.resolve({ duration: 10 }); }
  resume() { this.state = 'running'; }
  close() { this.closed = true; this.state = 'closed'; }
}

/* ------------------------------ harnais ---------------------------------- */
function buildMasteringHarness(fetchFn) {
  const els = new Map();
  const store = {};
  const requests = [];
  const doc = (sel) => {
    if (!els.has(sel)) els.set(sel, makeEl(sel.startsWith('#') ? sel.slice(1) : 'div'));
    return els.get(sel);
  };
  const window = { App: {}, AudioContext: FakeAudioContext, addEventListener() {}, location: { protocol: 'http:', host: 'testserver' } };
  window.window = window;
  const document = { querySelector: doc, querySelectorAll() { return []; }, createElement: makeEl, documentElement: makeEl('html'), body: makeEl('body'), addEventListener() {} };
  const sandbox = {
    window, document,
    navigator: { serviceWorker: { getRegistrations: async () => [] } },
    caches: { keys: async () => [], delete: async () => true },
    localStorage: {
      getItem(k) { return (k in store) ? store[k] : null; },
      setItem(k, v) { store[k] = String(v); },
      removeItem(k) { delete store[k]; },
    },
    setTimeout(f) { f(); return 0; }, clearTimeout() {},
    setInterval() { return 1; }, clearInterval() {},
    fetch: async (url, opts) => {
      requests.push({ url: String(url), opts: opts || {} });
      return fetchFn(url, opts || {});
    },
    WebSocket: class { constructor() { this.readyState = 0; } close() {} },
    FormData: class { append() {} },
    alert() {}, confirm() { return true; }, requestAnimationFrame() {},
    location: { protocol: 'http:', host: 'testserver' },
    URL, TextEncoder, TextDecoder, Blob,
  };
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'app.js' });
  const newEngine = (track) => {
    sandbox.window.__track = track;
    return vm.runInContext('new AudioEngine(window.__track)', sandbox);
  };
  const drain = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
  return { sandbox, doc, els, store, requests, window, newEngine, drain };
}

/* Stub fetch : routes { includes: pattern, value } ; `value` peut être un
   Error, un code HTTP (404/500), un JSON, ou un byte payload pour audio. */
function routeFetch(routes) {
  return async (url, opts) => {
    for (const r of routes) {
      if (url.includes(r.pattern)) {
        const v = r.value;
        if (v instanceof Error) throw v;
        if (v instanceof ArrayBuffer) {
          return { ok: true, status: 200, json: async () => ({}), text: async () => '', arrayBuffer: async () => v };
        }
        if (typeof v === 'number' && (v === 404 || v === 500)) {
          return { ok: false, status: v, statusText: String(v), text: async () => '' };
        }
        if (typeof v === 'function') return v(url, opts);
        return { ok: true, status: 200, json: async () => v, text: async () => '' };
      }
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
  };
}

const AUDIO = new ArrayBuffer(16);

function makeTrack(over = {}) {
  return {
    id: 't1', title: 'Titre', artist: 'Artiste', duration: 10,
    stems: ['vocals', 'drums'], files: {}, chords: { bpm: 120, bars: [] },
    ...over,
  };
}

/* Convertit l'option `opts.body` (JSON string) en objet. */
function postedJSON(req) {
  const o = req.opts || {};
  return (typeof o.body === 'string') ? JSON.parse(o.body) : (o.body || {});
}

/* ========================= Moteur audio (AudioEngine) ======================= */

test('load(): charge le master lorsqu’il existe déjà dans files.master', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
  ]));
  const engine = h.newEngine(makeTrack({ files: { master: '/data/t1/master.wav' } }));
  await engine.load();
  assert.strictEqual(engine.ready, true);
  assert.strictEqual(engine.masterLoaded, true);
  assert.ok(engine.masterTrack, 'masterTrack doit être chargé');
  // Le master a bien été requêté.
  assert.ok(h.requests.some(r => r.url.includes('/master.wav')));
  // Mode A par défaut : le master reste silencieux, les stems audibles.
  assert.strictEqual(engine._effectiveGain('vocals'), 1.0);
  assert.strictEqual(engine.masterTrack.gain.gain.value, 0);
});

test('setABMode: B silencie les stems et active le master, A inverse', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
  ]));
  const engine = h.newEngine(makeTrack({ files: { master: '/data/t1/master.wav' } }));
  await engine.load();

  engine.setABMode('B');
  assert.strictEqual(engine.abMode, 'B');
  assert.strictEqual(engine._effectiveGain('vocals'), 0, 'stems muets en mode B');
  assert.strictEqual(engine.masterTrack.gain.gain.value, 1, 'master actif');

  engine.setABMode('A');
  assert.strictEqual(engine.abMode, 'A');
  assert.strictEqual(engine._effectiveGain('vocals'), 1.0, 'stems audibles en mode A');
  assert.strictEqual(engine.masterTrack.gain.gain.value, 0, 'master muet en mode A');
});

test('setABMode B sans master chargé : les stems restent audibles (repli)', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
  ]));
  const engine = h.newEngine(makeTrack()); // pas de files.master
  await engine.load();
  engine.setABMode('B');
  assert.strictEqual(engine.masterLoaded, false);
  assert.strictEqual(engine._effectiveGain('vocals'), 1.0, 'pas de corruption du mix en mode B sans master');
});

test('loadMaster(): définit files.master, charge le master et ne casse pas la lecture', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
  ]));
  const engine = h.newEngine(makeTrack());
  await engine.load();
  assert.strictEqual(engine.masterLoaded, false);
  const ok = await engine.loadMaster('/data/t1/master.wav');
  assert.strictEqual(ok, true);
  assert.strictEqual(engine.masterLoaded, true);
  assert.strictEqual(engine.track.files.master, '/data/t1/master.wav');
});

test('loadMaster(): exécution pendant la lecture → resynchronise (spawn du master)', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
  ]));
  const engine = h.newEngine(makeTrack());
  await engine.load();
  engine.play();
  assert.strictEqual(engine.playing, true);
  await engine.loadMaster('/data/t1/master.wav');
  // En lecture, la source master est bien lancée (resynchronisation).
  assert.ok(engine.masterTrack && engine.masterTrack.src, 'la source master doit être démarrée en lecture');
});

test('destroy(): libère la piste masterisée', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
  ]));
  const engine = h.newEngine(makeTrack({ files: { master: '/data/t1/master.wav' } }));
  await engine.load();
  assert.strictEqual(engine.masterLoaded, true);
  engine.destroy();
  assert.strictEqual(engine.masterTrack, null);
  assert.strictEqual(engine.masterLoaded, false);
});

/* ============================ UI (buildPlayer) ============================= */

// Peuple les faders de stems (proxy) avec des valeurs 0..100.
function setFaders(h, values) {
  for (const [name, v] of Object.entries(values)) {
    h.doc(`#fd-${name}`).value = String(v);
  }
}

function reqFor(h, pattern) { return h.requests.filter(r => r.url.includes(pattern)); }

// Monte le lecteur et modélise l'état initial réel du HTML : le comparateur
// A/B démarre masqué (`class="ab-compare hidden"`). Le proxy `innerHTML` ne
// parse pas le marquage, on pose donc la classe « hidden » à la main avant que
// le chargement asynchrone du moteur ne déclenche le handler de révélation.
function buildUI(h, track) {
  h.sandbox.buildPlayer(track);
  h.doc('#ab-compare').classList.add('hidden');
  return h.doc('#ab-compare');
}

test('UI: clic « Masteriser » POST correct (volumes faders + preset), révèle A/B en mode B', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/api/mastering/presets', value: { presets: ['standard', 'rock', 'acoustic'] } },
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
    {
      pattern: '/api/tracks/t1/master',
      value: { ok: true, master_url: '/data/t1/master.wav' },
    },
  ]));
  const track = makeTrack();
  buildUI(h, track);
  await h.drain();

  setFaders(h, { vocals: 80, drums: 120 });
  h.doc('#preset-select').value = 'rock';

  const btn = h.doc('#btn-master');
  const p = btn.dispatch('click');
  if (p && p.then) await p;
  await h.drain();

  // Corps du POST mastering.
  const post = reqFor(h, '/api/tracks/t1/master');
  assert.strictEqual(post.length, 1, 'une seule requête de mastering');
  assert.strictEqual(post[0].opts.method, 'POST');
  const body = postedJSON(post[0]);
  assert.deepStrictEqual(body.volumes, { vocals: 0.8, drums: 1.2 });
  assert.strictEqual(body.preset, 'rock');

  // Réponse → master chargé, comparateur révélé, mode B, téléchargement.
  const engine = h.sandbox.window.App.engine;
  assert.strictEqual(engine.masterLoaded, true);
  assert.strictEqual(engine.abMode, 'B');
  assert.strictEqual(h.doc('#ab-compare').classList.contains('hidden'), false);
  assert.strictEqual(h.doc('#ab-badge').textContent, 'B');
  assert.strictEqual(h.doc('#ab-toggle').getAttribute('aria-pressed'), 'true');
  assert.strictEqual(h.doc('#btn-download-master').href, '/data/t1/master.wav');
  assert.ok(/Master prêt/.test(h.doc('#mastering-hint').textContent));
});

test('UI: échec du mastering → indicateur en erreur, pas de comparateur', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/api/mastering/presets', value: { presets: ['standard', 'rock', 'acoustic'] } },
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/api/tracks/t1/master', value: 500 },
  ]));
  buildUI(h, makeTrack());
  await h.drain();

  const btn = h.doc('#btn-master');
  const p = btn.dispatch('click');
  if (p && p.then) await p;
  await h.drain();

  assert.strictEqual(h.doc('#mastering-hint').classList.contains('err'), true);
  assert.ok(/Échec du mastering/.test(h.doc('#mastering-hint').textContent));
  assert.strictEqual(h.doc('#ab-compare').classList.contains('hidden'), true, 'le comparateur reste masqué si aucun master');
  const engine = h.sandbox.window.App.engine;
  assert.strictEqual(engine.abMode, 'A', 'reste en mix brut après échec');
  assert.strictEqual(h.doc('#btn-master').disabled, false, 'bouton réactivé après échec');
});

test('UI: bascule A/B toggle alterne mode et états visuels', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/api/mastering/presets', value: { presets: ['standard', 'rock', 'acoustic'] } },
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
    { pattern: '/api/tracks/t1/master', value: { ok: true, master_url: '/data/t1/master.wav' } },
  ]));
  const track = makeTrack();
  buildUI(h, track);
  await h.drain();

  // Génère un master → mode B actif.
  const p = h.doc('#btn-master').dispatch('click');
  if (p && p.then) await p;
  await h.drain();
  const engine = h.sandbox.window.App.engine;
  assert.strictEqual(engine.abMode, 'B');

  // Bascule A/B.
  const toggle = h.doc('#ab-toggle');
  toggle.dispatch('click');
  assert.strictEqual(engine.abMode, 'A');
  assert.strictEqual(h.doc('#ab-badge').textContent, 'A');
  assert.strictEqual(h.doc('#ab-toggle').getAttribute('aria-pressed'), 'false');
  assert.strictEqual(h.doc('#ab-toggle').classList.contains('mode-b'), false);
  assert.strictEqual(h.doc('#ab-label-a').classList.contains('active'), true);

  // Rebascule en B.
  toggle.dispatch('click');
  assert.strictEqual(engine.abMode, 'B');
  assert.strictEqual(h.doc('#ab-badge').textContent, 'B');
  assert.strictEqual(h.doc('#ab-toggle').getAttribute('aria-pressed'), 'true');
});

test('UI: un master déjà présent est révélé à l’ouverture (reste en mode A)', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/api/mastering/presets', value: { presets: ['standard', 'rock', 'acoustic'] } },
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    { pattern: '/master.wav', value: AUDIO },
  ]));
  const track = makeTrack({ files: { master: '/data/t1/master.wav' } });
  buildUI(h, track);
  await h.drain();
  await h.drain();

  const engine = h.sandbox.window.App.engine;
  assert.strictEqual(engine.masterLoaded, true);
  assert.strictEqual(h.doc('#ab-compare').classList.contains('hidden'), false);
  // Aucun clic : on reste sur le mix brut (mode A).
  assert.strictEqual(engine.abMode, 'A');
  assert.strictEqual(h.doc('#ab-badge').textContent, 'A');
  assert.strictEqual(h.doc('#btn-download-master').href, '/data/t1/master.wav');
});

test('UI: populates presets via /api/mastering/presets', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/api/mastering/presets', value: { presets: ['standard', 'rock', 'acoustic'] } },
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
  ]));
  buildUI(h, makeTrack());
  await h.drain();
  const sel = h.doc('#preset-select');
  assert.ok(/standard/i.test(sel.innerHTML));
  assert.ok(/rock/i.test(sel.innerHTML));
  assert.strictEqual(sel.value, 'standard', 'le premier preset est sélectionné par défaut');
});

test('UI: les presets retombent sur la liste canonique si l’API est indisponible', async () => {
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/api/mastering/presets', value: new Error('offline') },
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
  ]));
  buildUI(h, makeTrack());
  await h.drain();
  const sel = h.doc('#preset-select');
  // Le repli hors-ligne doit rester aligné sur REFERENCE_PRESETS du backend,
  // donc inclure « reggae » (présence exigée).
  assert.match(sel.innerHTML, /standard/);
  assert.match(sel.innerHTML, /rock/);
  assert.match(sel.innerHTML, /reggae/);
  assert.strictEqual(sel.value, 'standard');
});

test('UI: le POST de mastering se désactive pendant l’opération (anti double-clic)', async () => {
  let resolvePost;
  const h = buildMasteringHarness(routeFetch([
    { pattern: '/api/mastering/presets', value: { presets: ['standard', 'rock', 'acoustic'] } },
    { pattern: '/stems/vocals.mp3', value: AUDIO },
    { pattern: '/stems/drums.mp3', value: AUDIO },
    {
      pattern: '/api/tracks/t1/master',
      value: () => new Promise((resolve) => { resolvePost = () => resolve({ ok: true, json: async () => ({ ok: true, master_url: '/data/t1/master.wav' }) }); }),
    },
  ]));
  buildUI(h, makeTrack());
  await h.drain();

  const btn = h.doc('#btn-master');
  // Pendant que le POST est en attente, le bouton est désactivé (anti double-clic).
  const p = btn.dispatch('click');
  assert.strictEqual(btn.disabled, true, 'bouton désactivé pendant le mastering');
  assert.strictEqual(btn.classList.contains('loading'), true);

  // On résout la requête, puis le bouton redevient actif après l’opération.
  resolvePost();
  await (p && p.then ? p : Promise.resolve());
  await h.drain();
  assert.strictEqual(btn.disabled, false, 'bouton réactivé en fin de mastering');
  assert.strictEqual(btn.classList.contains('loading'), false);
});
