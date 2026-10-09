'use strict';
/* Tests T-UI-08 (frontend) : refonte ergonomique responsive de la bibliothèque.
 *
 * Charge `static/app.js` dans un harnais Node (`vm` + DOM simulé par Proxy) :
 * aucun navigateur, aucune dépendance externe.
 *
 * On couvre la logique pure et le rendu introduits par la refonte :
 *   * `cleanTitle` : nettoyage des mentions parasites des titres YouTube
 *     (« [Official Video] », « (Lyrics) », « - Official Audio », …), repli
 *     anti-titre-vide (« Titre sans nom » / titre brut) et non-régression ;
 *   * `renderCard` : carte épurée — plus de badge/chip, de croix, ni de bouton
 *     « Ouvrir le labo » ; carte entièrement cliquable (`role="button"`),
 *     pastille de statut, bouton d'actions « ••• », classe `not-ready` ;
 *   * `toggleCardMenu` / `closeCardMenu` : menu contextuel flottant (un seul
 *     ouvert à la fois) avec Changer de dossier / Renommer / Supprimer ;
 *   * `openFolderAssignDialog` : assignation de dossier (options + sélection
 *     courante + échappement anti-injection) ;
 *   * `bindCardEvents` : le clic ouvre le lecteur sauf sur le bouton « ••• »,
 *     le clavier (Entrée / Espace) ouvre aussi le lecteur ;
 *   * actions réelles du menu « ••• » : Supprimer → `API.remove` + refresh,
 *     Renommer / Changer de dossier → ouverture des dialogues, « Enregistrer »
 *     → `API.updateTrackMeta` + refresh ;
 *   * fermeture du menu au clic extérieur (handler délégué de document).
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const CODE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'app.js'), 'utf8');

/* ------------------------- DOM simulé minimal ---------------------------- */
function makeEl(tag = 'div') {
  const t = {
    id: '', tagName: tag.toUpperCase(), value: '', files: [], textContent: '',
    innerHTML: '', className: '', title: '', dataset: {}, children: [],
    _parent: null, _l: {},
    scrollTop: 0, scrollHeight: 0, offsetHeight: 10,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; }, replace() {} },
    style: { setProperty() {}, getPropertyValue() { return ''; }, width: '', height: '', left: '', top: '' },
    appendChild(c) { c._parent = this; this.children.push(c); return c; },
    // remove() détache réellement l'élément de son parent → permet de vérifier
    // la fermeture du menu contextuel (un seul ouvert à la fois).
    remove() {
      if (this._parent) {
        const i = this._parent.children.indexOf(this);
        if (i >= 0) this._parent.children.splice(i, 1);
        this._parent = null;
      }
    },
    addEventListener(ev, f) { (this._l[ev] = this._l[ev] || []).push(f); },
    removeEventListener(ev, f) { const a = this._l[ev]; if (a) { const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getAttribute() { return null; }, setAttribute() {}, removeAttribute() {},
    getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
    click() {}, closest() { return null; }, contains(c) { return c && this.children.includes(c); }, focus() {},
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
    innerWidth: 1200,
    App: {},
  };
  const docClickHandlers = [];
  const document = {
    querySelector: doc,
    querySelectorAll() { return []; },
    createElement: makeEl,
    documentElement: makeEl('html'),
    body: makeEl('body'),
    addEventListener(ev, f) { if (ev === 'click') docClickHandlers.push(f); },
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
    document, window, docClickHandlers,
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
    if (url.includes('/api/tracks')) {
      return { ok: true, status: 200, json: async () => [], text: async () => '' };
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
  };
}

/* Stub fetch qui enregistre les requêtes (url / méthode / corps) pour pouvoir
   vérifier les appels réels à `API.remove` / `API.updateTrackMeta` sans accéder
   à l'objet `API` (non exposé par le sandbox). */
function recordFetch(routes, log) {
  return async (url, opts) => {
    log.push({ url, method: opts && opts.method, body: opts && opts.body });
    for (const [pattern, val] of routes) {
      if (url.includes(pattern)) {
        if (val instanceof Error) throw val;
        if (typeof val === 'number' && (val === 404 || val === 500)) {
          return { ok: false, status: val, statusText: String(val), text: async () => '' };
        }
        return { ok: true, status: 200, json: async () => val, text: async () => '' };
      }
    }
    if (url.includes('/api/tracks')) {
      return { ok: true, status: 200, json: async () => [], text: async () => '' };
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
  };
}

// Drainage des microtâches/promesses non attendues.
const drain = () => new Promise((r) => setImmediate(r));

/* Fabrique un événement de clic dont la cible simule une entrée du menu « ••• »
   (le `closest('.card-menu-item')` renvoie l'entrée elle-même). */
function menuItemEvent(action) {
  const item = { dataset: { action }, closest(sel) { return (sel === '.card-menu-item' ? item : null); } };
  return { target: item, stopPropagation() {} };
}
// Bouton « ••• » factice avec géométrie pour le positionnement du menu.
function menuButton(left = 10) {
  const b = makeEl('button');
  b.getBoundingClientRect = () => ({ left, top: 40, right: left + 30, bottom: 70, width: 30, height: 30 });
  return b;
}
// Carte factice porteuse du `dataset` lu par le menu.
function menuCard(over = {}) {
  const c = makeEl('article');
  c.dataset = Object.assign({ id: 't1', title: 'Titre', artist: 'Artiste', folder: 'Rock' }, over);
  return c;
}

function track(id, folder, opts = {}) {
  return Object.assign({ id, title: `Titre ${id}`, artist: '', status: 'ready',
                         duration: null, bpm: null, thumbnail: null, stems: [], source: '' },
                       opts, { folder });
}

/* ================================ Tests ================================== */

/* ------------------------- cleanTitle (logique pure) ---------------------- */
test('cleanTitle: supprime un bloc (…) avec un mot parasite', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song (Official Video)'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Wonderwall (Official Video)'), 'Wonderwall');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Lyrics Video)'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song (4K Remaster)'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Dance Mix)'), 'Song');
});

test('cleanTitle: supprime un bloc […] avec un mot parasite', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song [Official Music Video]'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song [4K]'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Official Video Remastered) [HD]'), 'Song');
});

test('cleanTitle: supprime les parasites de fin de titre non parenthésés', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song - Official Video'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song | Official Video'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song · Official Audio'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song — Remastered'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Lyrics'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Mv'), 'Song');
});

test('cleanTitle: combine plusieurs parasites et normalise les espaces', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Artist - Song (Official Video) (Lyrics)'), 'Artist - Song');
  assert.strictEqual(h.sandbox.cleanTitle('Artist   -   Song   (Official Video)'), 'Artist - Song');
  assert.strictEqual(h.sandbox.cleanTitle('Artist   —   Song   (Official Video)'), 'Artist — Song');
});

test('cleanTitle: conserve les parenthèses et fins de titre légitimes (non-régression)', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song (feat. Artist)'), 'Song (feat. Artist)');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Live'), 'Song - Live');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Instrumental'), 'Song - Instrumental');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Live at Wembley'), 'Song - Live at Wembley');
  assert.strictEqual(h.sandbox.cleanTitle('Nothing'), 'Nothing');
  // Un suffixe parasite résiduel hors de la fin n'est pas tronqué à tort.
  assert.strictEqual(h.sandbox.cleanTitle('Song - Remastered 2024'), 'Song - Remastered 2024');
});

/* --------------- frontières de mots parasites (non-régression) --------------- */
test('cleanTitle: conserve les faux positifs des mots ambigus (Mixing/HDTV/Musical/MVG)', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song (Mixing by John)'), 'Song (Mixing by John)');
  assert.strictEqual(h.sandbox.cleanTitle('Song (HDTV Rip)'), 'Song (HDTV Rip)');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Musical)'), 'Song (Musical)');
  assert.strictEqual(h.sandbox.cleanTitle('Song (MVG Best)'), 'Song (MVG Best)');
});

test('cleanTitle: exclut Editor / Editorial (mot « edit » resté borné)', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song (Editor)'), 'Song (Editor)');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Editorial)'), 'Song (Editorial)');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Editor Cut)'), 'Song (Editor Cut)');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Editorial'), 'Song - Editorial');
});

test('cleanTitle: nettoie les formes infectées de « edit » (Edit/Edited/Editing)', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song (Edit)'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Edited)'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song (Editing)'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song [Edit]'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song [Edited]'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Edit'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Edited'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Editing'), 'Song');
});

test('cleanTitle: nettoie les formes parasites bornées (Official Mix / HD Video)', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song (Official Mix)'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song (HD Video)'), 'Song');
});

test('cleanTitle: nettoie les suffixes parasites tolérés (remastered / lyrics / hd / music)', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('Song - Remastered'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - HD'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Music'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song - Official HD Video'), 'Song');
  assert.strictEqual(h.sandbox.cleanTitle('Song | Lyrics'), 'Song');
});

/* ----------------------- cleanTitle : repli anti-titre-vide ----------------- */
test('cleanTitle: alimente un libellé neutre pour un titre vide', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle(''), 'Titre sans nom');
  assert.strictEqual(h.sandbox.cleanTitle(null), 'Titre sans nom');
  assert.strictEqual(h.sandbox.cleanTitle(undefined), 'Titre sans nom');
  assert.strictEqual(h.sandbox.cleanTitle('   '), 'Titre sans nom');
});

test('cleanTitle: un titre composé uniquement de tags parasites retombe sur le titre brut', () => {
  const h = buildHarness(routeFetch([]));
  assert.strictEqual(h.sandbox.cleanTitle('[Official Video]'), '[Official Video]');
  assert.strictEqual(h.sandbox.cleanTitle('(Lyrics)'), '(Lyrics)');
  assert.strictEqual(h.sandbox.cleanTitle('(Official Video Remastered) [HD]'), '(Official Video Remastered) [HD]');
});



/* ------------------------- renderCard (rendu HTML) ------------------------ */
test('renderCard: carte épurée — plus de chip/croix/option/bouton « Ouvrir le labo »', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.renderCard(track('a', 'Rock', { title: 'Song [Official Video]', status: 'ready' }));
  assert.doesNotMatch(html, /card-del/);        // plus de croix superposée
  assert.doesNotMatch(html, /class="chip|class='chip/); // plus de badge « PRÊT »
  assert.doesNotMatch(html, /<option/);         // plus de <option> sur la carte
  assert.doesNotMatch(html, /card-edit/);       // plus de bouton « renommer »
  assert.doesNotMatch(html, /folder-select/);   // plus de <select> sur la carte
  assert.doesNotMatch(html, /Ouvrir le labo/);  // plus de bouton « Ouvrir le labo »
});

test('renderCard: contient la pastille de statut, le bouton ••• et la pochette', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.renderCard(track('a', '', { title: 'Song', status: 'ready' }));
  assert.match(html, /card-menu-btn/);          // bouton d'actions contextuel
  assert.match(html, /•••/);
  assert.match(html, /status-dot ready/);       // pastille « Prêt »
  assert.match(html, /card-play/);              // overlay play au survol
  assert.match(html, /class="card(?! not-ready)/); // pas de classe not-ready pour un morceau prêt
  assert.match(html, /data-id="a"/);
  assert.match(html, /role="button"/);
  assert.match(html, /tabindex="0"/);
});

test('renderCard: pastille et classe not-ready pour un morceau en erreur', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.renderCard(track('e', '', { title: 'Err', status: 'error' }));
  assert.match(html, /class="card not-ready"/);
  assert.match(html, /status-dot error/);
});

test('renderCard: pastille « processing » pour un statut autre que ready/error', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.renderCard(track('p', '', { title: 'Proc', status: 'processing' }));
  assert.match(html, /class="card not-ready"/);
  assert.match(html, /status-dot processing/);
});

test('renderCard: le titre est nettoyé dans data-title, h3 et aria-label', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.renderCard(track('t', '', { title: 'Titre - (Official Video)', status: 'ready' }));
  assert.match(html, /data-title="Titre"/);
  assert.match(html, /<h3 title="Titre">Titre<\/h3>/);
  // Le libellé d'accessibilité réutilise le titre nettoyé.
  assert.match(html, /aria-label="Ouvrir Titre dans le labo"/);
});

test('renderCard: échappe les caractères HTML du titre (anti-injection)', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.renderCard(track('x', '', { title: '<img src=x onerror=alert(1)>', status: 'ready' }));
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img/);
  assert.match(html, /data-title="&lt;img src=x onerror=alert\(1\)&gt;"/);
});

test('renderCard: montre un fallback quand il n\'y a pas de vignette', () => {
  const h = buildHarness(routeFetch([]));
  const html = h.sandbox.renderCard(track('n', '', { title: 'N', status: 'ready', thumbnail: null }));
  assert.match(html, /class="fallback"/);
  assert.doesNotMatch(html, /background-image/);
});

/* ------------------------- toggleCardMenu / closeCardMenu ----------------- */
test('toggleCardMenu: ouvre le menu flottant avec les trois actions', () => {
  const h = buildHarness(routeFetch([]));
  const btn = makeEl('button');
  btn.getBoundingClientRect = () => ({ left: 100, top: 40, right: 130, bottom: 70, width: 30, height: 30 });
  const card = makeEl('article');
  card.dataset.id = 'a';
  h.sandbox.toggleCardMenu(btn, card);
  const dropdown = h.document.body.children[0];
  assert.ok(dropdown, 'le menu doit être créé');
  assert.strictEqual(dropdown.className, 'card-menu-dropdown');
  assert.match(dropdown.innerHTML, /Changer de dossier/);
  assert.match(dropdown.innerHTML, /Renommer/);
  assert.match(dropdown.innerHTML, /data-action="delete"/);
  assert.match(dropdown.innerHTML, /class="card-menu-item danger"/);
  assert.strictEqual(dropdown.style.left, '92px'); // positionné près du bouton
});

test('toggleCardMenu: un seul menu ouvert à la fois', () => {
  const h = buildHarness(routeFetch([]));
  const card = makeEl('article');
  const mkBtn = (v) => { const b = makeEl('button'); b.getBoundingClientRect = () => ({ left: v, top: 0, right: v, bottom: 0, width: 0, height: 0 }); return b; };
  h.sandbox.toggleCardMenu(mkBtn(10), card);
  h.sandbox.toggleCardMenu(mkBtn(30), card);
  assert.strictEqual(h.document.body.children.length, 1, 'le menu précédent doit être fermé');
});

test('closeCardMenu: détache le menu du DOM', () => {
  const h = buildHarness(routeFetch([]));
  const btn = makeEl('button');
  const card = makeEl('article');
  h.sandbox.toggleCardMenu(btn, card);
  assert.strictEqual(h.document.body.children.length, 1);
  h.sandbox.closeCardMenu();
  assert.strictEqual(h.document.body.children.length, 0);
});

/* ------------------------- openFolderAssignDialog -------------------------- */
test('openFolderAssignDialog: liste les dossiers et sélectionne le courant', () => {
  const h = buildHarness(routeFetch([]));
  h.sandbox.window.App.folders = ['Rock', 'Jazz', 'Blues'];
  h.sandbox.openFolderAssignDialog('a', 'Jazz');
  const veil = h.document.body.children[0];
  assert.ok(veil, 'le dialogue doit être créé');
  assert.match(veil.innerHTML, /Changer de dossier/);
  assert.match(veil.innerHTML, /<option value="">Non classé<\/option>/);
  assert.match(veil.innerHTML, /<option value="Rock"/);
  assert.match(veil.innerHTML, /<option value="Jazz" selected>/); // dossier courant
  assert.match(veil.innerHTML, /id="folder-assign-save"/);
});

test('openFolderAssignDialog: échappe les noms de dossiers (anti-injection)', () => {
  const h = buildHarness(routeFetch([]));
  h.sandbox.window.App.folders = ['<img src=x>'];
  h.sandbox.openFolderAssignDialog('a', '');
  const veil = h.document.body.children[0];
  assert.doesNotMatch(veil.innerHTML, /<img src=x/);
  assert.match(veil.innerHTML, /&lt;img/);
});

/* ------------------------- bindCardEvents (routage clic) ------------------ */
test('bindCardEvents: un clic hors menu ouvre le lecteur', async () => {
  const h = buildHarness(routeFetch([]));
  const calls = [];
  h.sandbox.openPlayer = async (id) => { calls.push(id); };
  const card = makeEl('article');
  card.dataset.id = 't1';
  h.sandbox.bindCardEvents(card);
  const clickHandler = card._l.click[0];
  // Clic sur la carte (target = la carte elle-même) → ouvre le lecteur.
  await clickHandler({ target: card });
  assert.deepStrictEqual(calls, ['t1']);
});

test('bindCardEvents: un clic sur le bouton ••• ne déclenche pas le lecteur', async () => {
  const h = buildHarness(routeFetch([]));
  const calls = [];
  h.sandbox.openPlayer = async (id) => { calls.push(id); };
  const menuBtn = makeEl('button');
  const card = makeEl('article');
  card.dataset.id = 't1';
  card.querySelector = (sel) => (sel === '.card-menu-btn' ? menuBtn : null);
  menuBtn.closest = (sel) => (sel === '.card-menu-btn' ? menuBtn : null);
  h.sandbox.bindCardEvents(card);
  const clickHandler = card._l.click[0];
  await clickHandler({ target: menuBtn });
  assert.deepStrictEqual(calls, [], 'le lecteur ne doit pas être ouvert');
});

test('bindCardEvents: Entrée / Espace ouvrent aussi le lecteur', async () => {
  const h = buildHarness(routeFetch([]));
  const calls = [];
  h.sandbox.openPlayer = async (id) => { calls.push(id); };
  const card = makeEl('article');
  card.dataset.id = 't1';
  card.click = () => { calls.push('click'); };
  h.sandbox.bindCardEvents(card);
  const keyHandler = card._l.keydown[0];
  let prevented = false;
  keyHandler({ target: card, key: 'Enter', preventDefault: () => { prevented = true; } });
  assert.ok(prevented);
  assert.deepStrictEqual(calls, ['click']);
  // Autre touche : ne doit rien déclencher.
  keyHandler({ target: card, key: 'a', preventDefault: () => {} });
  assert.deepStrictEqual(calls, ['click']);
});

test('bindCardEvents: un clic sur le menu ••• ouvre le menu contextuel', async () => {
  const h = buildHarness(routeFetch([]));
  const menuBtn = makeEl('button');
  const card = makeEl('article');
  card.dataset.id = 't1';
  card.querySelector = (sel) => (sel === '.card-menu-btn' ? menuBtn : null);
  menuBtn.closest = (sel) => (sel === '.card-menu-btn' ? menuBtn : null);
  let stopped = false;
  menuBtn.addEventListener = (ev, f) => {
    if (ev === 'click') menuBtn.__onClick = (e) => { e.stopPropagation(); f(e); };
  };
  h.sandbox.bindCardEvents(card);
  h.sandbox.toggleCardMenu = (btn, c) => { menuBtn.__open = { btn, c }; };
  menuBtn.__onClick({ stopPropagation: () => { stopped = true; } });
  assert.ok(stopped, 'le clic sur ••• doit être isolé');
  assert.strictEqual(menuBtn.__open.btn, menuBtn);
  assert.strictEqual(menuBtn.__open.c, card);
});

/* --------------- intégration renderCards (remplace les assertions obsolètes) */
test('refreshLibrary: rend une carte épurée avec le bouton d\'actions et pastille', async () => {
  const h = buildHarness(routeFetch([
    ['/api/tracks', [track('a', 'Rock', { title: 'Premier (Official Video)' }),
                     track('b', '', { title: 'Non classé' })]],
    ['/api/folders', ['Rock']],
  ]));
  await h.sandbox.refreshLibrary();
  await drain();
  const lib = h.doc('#library');
  // Le titre est nettoyé dans data-title.
  assert.match(lib.innerHTML, /data-title="Premier"/);
  assert.doesNotMatch(lib.innerHTML, /data-title="Premier \(Official Video\)"/);
  // Le sélecteur de dossier / bouton « renommer » ont disparu de la carte.
  assert.doesNotMatch(lib.innerHTML, /<option value="Rock"/);
  assert.doesNotMatch(lib.innerHTML, /card-edit/);
  assert.doesNotMatch(lib.innerHTML, /folder-select/);
  assert.doesNotMatch(lib.innerHTML, /Ouvrir le labo/);
  // Nouvelle structure : bouton d'actions « ••• » + pastille de statut.
  assert.match(lib.innerHTML, /card-menu-btn/);
  assert.match(lib.innerHTML, /status-dot ready/);
  assert.strictEqual(h.doc('#lib-count').textContent, '2 morceaux');
});

/* ------------- actions réelles du menu contextuel « ••• » ----------------- */
test('menu ••• : clic sur « Supprimer » → API.remove + refreshLibrary', async () => {
  const log = [];
  const h = buildHarness(recordFetch([['/api/tracks/t1', {}]], log));
  let refreshed = false;
  h.sandbox.refreshLibrary = async () => { refreshed = true; };
  h.sandbox.toggleCardMenu(menuButton(), menuCard());
  const dropdown = h.document.body.children[0];
  await dropdown._l.click[0](menuItemEvent('delete'));
  await drain(); await drain();
  // API.remove(id) → DELETE /api/tracks/t1.
  assert.strictEqual(log.filter((r) => r.method === 'DELETE' && r.url === '/api/tracks/t1').length, 1);
  assert.ok(refreshed, 'refreshLibrary doit être appelé après suppression');
  // Le menu est refermé après un clic sur une entrée.
  assert.strictEqual(h.document.body.children.length, 0);
});

test('menu ••• : clic sur « Renommer » → openRenameDialog ouvert', () => {
  const h = buildHarness(routeFetch([]));
  let args = null;
  h.sandbox.openRenameDialog = (id, title, artist) => { args = { id, title, artist }; };
  h.sandbox.toggleCardMenu(menuButton(), menuCard());
  const dropdown = h.document.body.children[0];
  dropdown._l.click[0](menuItemEvent('rename'));
  assert.deepStrictEqual(args, { id: 't1', title: 'Titre', artist: 'Artiste' });
});

test('menu ••• : clic sur « Changer de dossier » → openFolderAssignDialog ouvert', () => {
  const h = buildHarness(routeFetch([]));
  let args = null;
  h.sandbox.openFolderAssignDialog = (id, folder) => { args = { id, folder }; };
  h.sandbox.toggleCardMenu(menuButton(), menuCard({ folder: 'Jazz' }));
  const dropdown = h.document.body.children[0];
  dropdown._l.click[0](menuItemEvent('folder'));
  assert.deepStrictEqual(args, { id: 't1', folder: 'Jazz' });
});

/* ------------------- boutons « Enregistrer » des dialogues ----------------- */
test('dialogue Renommer : « Enregistrer » → API.updateTrackMeta({title, artist}) + refreshLibrary', async () => {
  const log = [];
  const h = buildHarness(recordFetch([['/api/tracks/t1', {}]], log));
  let refreshed = false;
  h.sandbox.refreshLibrary = async () => { refreshed = true; };
  const inputTitle = makeEl('input'); inputTitle.value = 'Nouveau titre';
  const inputArtist = makeEl('input'); inputArtist.value = 'Nouvel artiste';
  const saveBtn = makeEl('button');
  const veil = makeEl('div');
  veil.querySelector = (sel) => {
    if (sel === '#rename-title') return inputTitle;
    if (sel === '#rename-artist') return inputArtist;
    if (sel === '#rename-save') return saveBtn;
    return null;
  };
  h.sandbox.document.createElement = () => veil;
  h.sandbox.openRenameDialog('t1', 'Titre', 'Artiste');
  await saveBtn._l.click[0]();
  await drain(); await drain();
  const patch = log.find((r) => r.method === 'PATCH' && r.url === '/api/tracks/t1');
  assert.ok(patch, 'PATCH /api/tracks/t1 attendu');
  assert.deepStrictEqual(JSON.parse(patch.body), { title: 'Nouveau titre', artist: 'Nouvel artiste' });
  assert.ok(refreshed, 'refreshLibrary doit être appelé après renommage');
  assert.strictEqual(h.document.body.children.length, 0, 'la modale doit être fermée');
});

test('dialogue Renommer : « Enregistrer » refuse un titre vide', async () => {
  const log = [];
  const h = buildHarness(recordFetch([], log));
  let alertMsg = null;
  h.sandbox.alert = (m) => { alertMsg = m; };
  const inputTitle = makeEl('input'); inputTitle.value = '   ';
  const inputArtist = makeEl('input'); inputArtist.value = 'A';
  const saveBtn = makeEl('button');
  const veil = makeEl('div');
  veil.querySelector = (sel) => {
    if (sel === '#rename-title') return inputTitle;
    if (sel === '#rename-artist') return inputArtist;
    if (sel === '#rename-save') return saveBtn;
    return null;
  };
  h.sandbox.document.createElement = () => veil;
  h.sandbox.openRenameDialog('t1', 'Titre', 'Artiste');
  await saveBtn._l.click[0]();
  await drain();
  assert.match(alertMsg, /vide/);
  assert.strictEqual(log.filter((r) => r.method === 'PATCH').length, 0, 'aucun PATCH ne doit partir');
});

test('dialogue Changer de dossier : « Enregistrer » → API.updateTrackMeta({folder}) + refreshLibrary', async () => {
  const log = [];
  const h = buildHarness(recordFetch([['/api/tracks/t1', {}]], log));
  let refreshed = false;
  h.sandbox.refreshLibrary = async () => { refreshed = true; };
  h.sandbox.window.App.folders = ['Rock', 'Jazz'];
  const select = makeEl('select'); select.value = 'Jazz';
  const saveBtn = makeEl('button');
  const veil = makeEl('div');
  veil.querySelector = (sel) => {
    if (sel === '#folder-assign') return select;
    if (sel === '#folder-assign-save') return saveBtn;
    return null;
  };
  h.sandbox.document.createElement = () => veil;
  h.sandbox.openFolderAssignDialog('t1', 'Rock');
  await saveBtn._l.click[0]();
  await drain(); await drain();
  const patch = log.find((r) => r.method === 'PATCH' && r.url === '/api/tracks/t1');
  assert.ok(patch, 'PATCH /api/tracks/t1 attendu');
  assert.deepStrictEqual(JSON.parse(patch.body), { folder: 'Jazz' });
  assert.ok(refreshed, 'refreshLibrary doit être appelé après assignation');
  assert.strictEqual(h.document.body.children.length, 0, 'la modale doit être fermée');
});

/* ------------------- fermeture du menu au clic extérieur ------------------- */
test('clic extérieur : le handler délégué de document ferme le menu « ••• »', async () => {
  const h = buildHarness(routeFetch([]));
  await h.savedInit(); // init() — enregistre le handler délégué de document
  assert.strictEqual(h.docClickHandlers.length, 1, 'le handler délégué doit être enregistré');
  h.sandbox.toggleCardMenu(menuButton(), menuCard());
  assert.strictEqual(h.document.body.children.length, 1);
  // Clic hors du menu (cible quelconque, pas un bouton •••) → fermeture.
  const outside = { closest: () => null };
  h.docClickHandlers[0]({ target: outside });
  assert.strictEqual(h.document.body.children.length, 0, 'le menu doit être retiré du DOM');
});

test('clic extérieur : un clic sur un bouton ••• ne ferme pas le menu', async () => {
  const h = buildHarness(routeFetch([]));
  await h.savedInit();
  h.sandbox.toggleCardMenu(menuButton(), menuCard());
  // La cible est (ou est contenue dans) un bouton ••• → le menu reste ouvert.
  const onMenuBtn = { closest: (sel) => (sel === '.card-menu-btn' ? {} : null) };
  h.docClickHandlers[0]({ target: onMenuBtn });
  assert.strictEqual(h.document.body.children.length, 1, 'le menu ne doit pas se fermer');
});
