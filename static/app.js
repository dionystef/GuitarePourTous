'use strict';
/* ===========================================================================
   Guitar Lab — SPA / PWA. Lecteur multi-stems synchronisé, grille d'accords
   interactive, diagramme d'accords, boucles A/B, vitesse sans pitch.
   =========================================================================== */

/* ------------------------------- helpers --------------------------------- */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtTime = (t) => {
  if (!isFinite(t) || t < 0) return '0:00';
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
};

const API = {
  async json(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error((await r.text().catch(() => '')) || r.statusText);
    return r.json();
  },
  health:   () => API.json('/api/health'),
  tracks:   () => API.json('/api/tracks'),
  track:    (id) => API.json(`/api/tracks/${id}`),
  status:   (id) => API.json(`/api/status/${id}`),
  remove:   (id) => API.json(`/api/tracks/${id}`, { method: 'DELETE' }),
  process:  (body) => API.json('/api/process', { method: 'POST', body }),
};

/* ------------------------------- accords ---------------------------------- */
const FLATS = { Db: 'C#', Eb: 'D#', Gb: 'F#', Ab: 'G#', Bb: 'A#', Cb: 'B', Fb: 'E' };
const SEMITONE = { C: 0, 'C#': 1, D: 2, 'D#': 3, E: 4, F: 5, 'F#': 6, G: 7, 'G#': 8, A: 9, 'A#': 10, B: 11 };

const CHROMATIC_SHARP = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
const CHROMATIC_FLAT  = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B'];
function transposeChord(chordName, semitones, preferFlats = false) {
  if (!chordName || chordName === 'N' || semitones === 0) return chordName;
  const m = String(chordName).trim().replace(/\s+/g, '').match(/^([A-G](?:#|b)?)(.*)$/);
  if (!m) return chordName;
  const root = normRoot(m[1]);
  if (!(root in SEMITONE)) return chordName;
  const origIdx = SEMITONE[root];
  const newIdx = ((origIdx + semitones) % 12 + 12) % 12;
  const scale = preferFlats ? CHROMATIC_FLAT : CHROMATIC_SHARP;
  const newRoot = scale[newIdx];
  const suffix = m[2] || '';
  return newRoot + suffix;
}

function normRoot(r) { r = r[0].toUpperCase() + r.slice(1); return FLATS[r] || r; }

function parseChord(name) {
  name = String(name || 'N').trim().replace(/\s+/g, '');
  if (!name || name === 'N') return null;
  const m = name.match(/^([A-G](?:#|b)?)(.*)$/);
  if (!m) return null;
  const root = normRoot(m[1]);
  if (!(root in SEMITONE)) return null;
  const intervals = {
    '': [0, 4, 7], 'maj': [0, 4, 7], 'major': [0, 4, 7], 'm': [0, 3, 7],
    'min': [0, 3, 7], 'minor': [0, 3, 7], '7': [0, 4, 7, 10],
    'maj7': [0, 4, 7, 11], 'm7': [0, 3, 7, 10], '7sus4': [0, 5, 7, 10],
    'sus4': [0, 5, 7], 'sus2': [0, 2, 7], 'dim': [0, 3, 6], 'aug': [0, 4, 8],
    '6': [0, 4, 7, 9], 'm6': [0, 3, 7, 9], 'add9': [0, 4, 7, 14],
    '9': [0, 4, 7, 10, 14], 'm7b5': [0, 3, 6, 10], 'mmaj7': [0, 3, 7, 11],
    '5': [0, 7],
  };
  const inv = intervals[m[2].toLowerCase()] || [0, 4, 7, 10];
  return { name, root: SEMITONE[root], tones: new Set(inv.map(i => (SEMITONE[root] + i) % 12)) };
}

/* ------------- recherche de doigté (dictionnaire d'accords) --------------- */
// Doigtés standards EADGBe : `frets` = cases absolues (-1 = corde muette,
// 0 = à vide), `base` = case de la position (affichée dans le diagramme),
// `barre` = case de la barre éventuelle.
const GUITAR_CHORDS = {
  'C':     { frets: [-1, 3, 2, 0, 1, 0], base: 1 },
  'C#':    { frets: [-1, 4, 6, 6, 6, 4], base: 4, barre: 4 },
  'D':     { frets: [-1, -1, 0, 2, 3, 2], base: 1 },
  'Dm':    { frets: [-1, -1, 0, 2, 3, 1], base: 1 },
  'D7':    { frets: [-1, -1, 0, 2, 1, 2], base: 1 },
  'Dmaj7': { frets: [-1, -1, 0, 2, 2, 2], base: 1 },
  'Dsus4': { frets: [-1, -1, 0, 2, 3, 3], base: 1 },
  'E':     { frets: [0, 2, 2, 1, 0, 0], base: 1 },
  'Em':    { frets: [0, 2, 2, 0, 0, 0], base: 1 },
  'E7':    { frets: [0, 2, 0, 1, 0, 0], base: 1 },
  'F':     { frets: [1, 3, 3, 2, 1, 1], base: 1, barre: 1 },
  'Fm':    { frets: [1, 3, 3, 1, 1, 1], base: 1, barre: 1 },
  'F#':    { frets: [2, 4, 4, 3, 2, 2], base: 2, barre: 2 },
  'F#m':   { frets: [2, 4, 4, 2, 2, 2], base: 2, barre: 2 },
  'G':     { frets: [3, 2, 0, 0, 0, 3], base: 1 },
  'Gm':    { frets: [3, 5, 5, 3, 3, 3], base: 3, barre: 3 },
  'G7':    { frets: [3, 2, 0, 0, 0, 1], base: 1 },
  'G#':    { frets: [4, 6, 6, 5, 4, 4], base: 4, barre: 4 },
  'A':     { frets: [-1, 0, 2, 2, 2, 0], base: 1 },
  'Am':    { frets: [-1, 0, 2, 2, 1, 0], base: 1 },
  'A7':    { frets: [-1, 0, 2, 0, 2, 0], base: 1 },
  'Amaj7': { frets: [-1, 0, 2, 1, 2, 0], base: 1 },
  'B':     { frets: [-1, 2, 4, 4, 4, 2], base: 2, barre: 2 },
  'Bm':    { frets: [-1, 2, 4, 4, 3, 2], base: 2, barre: 2 },
  'Bb':    { frets: [-1, 1, 3, 3, 3, 1], base: 1, barre: 1 },
  'Cdim':  { frets: [-1, 3, 4, 2, 4, -1], base: 1 },
  'C#dim': { frets: [-1, 4, 5, 3, 5, -1], base: 3 },
  'Ddim':  { frets: [-1, -1, 0, 1, 3, 1], base: 1 },
  'D#dim': { frets: [-1, -1, 1, 2, 1, 2], base: 1 },
  'Edim':  { frets: [-1, -1, 2, 3, 2, 3], base: 1 },
  'Fdim':  { frets: [-1, -1, 3, 4, 3, 4], base: 1 },
  'F#dim': { frets: [-1, -1, 4, 5, 4, 5], base: 3 },
  'Gdim':  { frets: [-1, -1, 5, 6, 5, 6], base: 3 },
  'G#dim': { frets: [-1, -1, 0, 1, 0, 1], base: 1 },
  'Adim':  { frets: [-1, 0, 1, 2, 1, 2], base: 1 },
  'Bbdim': { frets: [-1, 1, 2, 0, 2, -1], base: 1 },
  'Bdim':  { frets: [-1, 2, 3, 1, 3, -1], base: 1 },
  'Csus4': { frets: [-1, 3, 3, 0, 1, 1], base: 1 },
  'C#sus4': { frets: [-1, 4, 6, 6, 7, 4], base: 4, barre: 4 },
  'Esus4': { frets: [0, 2, 2, 2, 0, 0], base: 1 },
  'Fsus4': { frets: [1, 3, 3, 3, 1, 1], base: 1, barre: 1 },
  'F#sus4': { frets: [2, 4, 4, 4, 2, 2], base: 2, barre: 2 },
  'Gsus4': { frets: [3, 3, 0, 0, 1, 3], base: 1 },
  'G#sus4': { frets: [4, 6, 6, 6, 4, 4], base: 4, barre: 4 },
  'Asus4': { frets: [-1, 0, 2, 2, 3, 0], base: 1 },
  'Bsus4': { frets: [-1, 2, 4, 4, 5, 2], base: 2, barre: 2 },
};
// Noms en dièses (rendus par parseChord) → équivalents bémols du dictionnaire.
const FLAT_OF = { 'C#': 'Db', 'D#': 'Eb', 'F#': 'Gb', 'G#': 'Ab', 'A#': 'Bb' };

function findFingering(chord) {
  if (!chord) return null;
  // La recherche tente d'abord le nom (dièse), puis sa version bémol.
  for (const name of [chord.name, FLAT_OF[chord.name]]) {
    const hit = GUITAR_CHORDS[name];
    if (!hit) continue;
    const position = hit.base || 1;
    // Cases absolues → cases relatives au diagramme de 5 cases.
    const frets = hit.frets.map(f => (f > 0 ? f - (position - 1) : f));
    return { frets, position, hasBarre: !!hit.barre };
  }
  return null;
}

function chordDiagramHTML(chordName) {
  const chord = parseChord(chordName);
  if (!chord) return '<div class="noc">N.C. — instrumental / piano</div>';
  const sol = findFingering(chord);
  if (!sol) return `<div class="name big">${esc(chord.name)}</div><div class="noc">Accord hors manche</div>`;

  const { frets, position } = sol;
  const W = 230, H = 148, px = 26, pyTop = 30, fretH = (H - pyTop - 12) / 5;
  const xs = i => px + i * ((W - 2 * px) / 5);
  const ys = n => pyTop + n * fretH;

  let s = `<svg class="neck" viewBox="0 0 ${W} ${H}" role="img" aria-label="Doigté ${esc(chord.name)}">`;
  for (let i = 0; i < 6; i++)
    s += `<line x1="${xs(i)}" y1="${ys(0)}" x2="${xs(i)}" y2="${ys(5)}" class="str${(i === 0 || i === 5) ? ' thick' : ''}"/>`;
  for (let n = 0; n <= 5; n++) {
    const w = n === 0 ? 4.5 : 1.3;
    s += `<line x1="${xs(0) - 5}" y1="${ys(n)}" x2="${xs(5) + 5}" y2="${ys(n)}" stroke="#3a4559" stroke-width="${w}"/>`;
  }
  if (position > 0) s += `<text x="${xs(0) - 11}" y="${ys(0) + fretH / 2 + 4}" class="fretnum">${position}</text>`;

  const barreMap = {};
  frets.forEach((f, i) => { if (f > 0) (barreMap[f] = barreMap[f] || []).push(i); });

  const covered = new Set();
  for (const [rel, idxs] of Object.entries(barreMap)) {
    if (idxs.length >= 2) {
      const first = idxs[0], last = idxs[idxs.length - 1];
      const top = ys(+rel) - fretH / 2 + 1.5;
      s += `<rect x="${xs(first) - 3.5}" y="${top}" width="${xs(last) - xs(first) + 7}" height="${fretH - 3}" class="barre" rx="3"/>`;
      idxs.forEach(i => covered.add(i));
    }
  }

  frets.forEach((f, i) => {
    if (covered.has(i)) return;
    if (f === 0) s += `<circle cx="${xs(i)}" cy="${ys(0) - 13}" r="7.5" fill="none" stroke="#5ee6c4" stroke-width="2.4"/>`;
    else if (f < 0) s += `<text x="${xs(i)}" y="${ys(0) - 12}" class="mute" text-anchor="middle">×</text>`;
    else s += `<circle cx="${xs(i)}" cy="${ys(f) + fretH / 2}" r="10.5" class="dot"/>`;
  });
  s += '</svg>';

  const root = chord.name.charAt(0).toUpperCase();
  const qual = chord.name.slice(1);
  return `<div class="chord-name"><span class="root">${esc(root)}</span>${esc(qual)}</div>${s}`;
}

/* ------------------------- canaux & visuels mixeur ------------------------ */
const CHANNELS = {
  mix:    { label: 'Mix',    color: 'var(--ch-mix)' },
  guitar: { label: 'Guitare', color: 'var(--ch-guitar)' },
  bass:   { label: 'Basse',   color: 'var(--ch-bass)' },
  drums:  { label: 'Batterie', color: 'var(--ch-drums)' },
  vocals: { label: 'Voix',    color: 'var(--ch-vocals)' },
  piano:  { label: 'Piano',   color: 'var(--ch-piano)' },
  other:  { label: 'Reste',   color: 'var(--ch-other)' },
};
const stemLabel = (n) => (CHANNELS[n] && CHANNELS[n].label) || n;
const stemColor = (n) => (CHANNELS[n] && CHANNELS[n].color) || 'var(--text-dim)';

/* ---------------------------------------------------------------------------
   Moteur audio : éléments <audio> synchronisés au pixel près (drift corriger),
   vitesse sans changement de hauteur (preservesPitch), boucle A/B.
   --------------------------------------------------------------------------- */
class AudioEngine {
  constructor(track) {
    this.track = track;
    this.stems = [];
    this.master = null;
    this.playing = false;
    this.speed = 1;
    this.muted = new Set();
    this.solo = null;
    this.loop = { on: false, a: 0, b: 0, barA: null, barB: null };
    this.masterVolume = 1.0;
    this.masterMuted = false;
    this.stemVolumes = {};
    this.ready = false;
    this.duration = track.duration || 0;
    this._drift = null;
  }

  get currentTime() { return this.ready && this.master ? this.master.el.currentTime : 0; }

  load() {
    this.stems = this.track.stems.map(name => {
      const el = new Audio(`/data/${this.track.id}/stems/${name}.mp3`);
      el.preload = 'auto';
      el.volume = 1;
      try { el.preservesPitch = true; el.mozPreservesPitch = true; el.webkitPreservesPitch = true; } catch (_) { /* ignore */ }
      return { name, el };
    });
    return Promise.all(this.stems.map(s => new Promise(res => {
      const ok = () => { s.loaded = true; res(); };
      s.el.addEventListener('loadeddata', () => { ok(); }, { once: true });
      s.el.addEventListener('canplay', () => { ok(); }, { once: true });
      s.el.addEventListener('error', () => { s.loaded = false; res(); }, { once: true });
      setTimeout(ok, 2000); // Sécurité anti-blocage
      try { s.el.load(); } catch (_) { res(); }
    }))).then(() => {
      const ds = this.stems.map(s => s.el.duration || 0).filter(Boolean);
      if (ds.length) this.duration = Math.max(...ds);
      this.master = this.stems[0];
      this.ready = true;
      this.refreshMix();
    });
  }

  play() {
    if (!this.ready || this.playing) return;
    this.playing = true;
    for (const s of this.stems) {
      s.el.playbackRate = this.speed;
      try { s.el.play(); } catch (_) { /* autoplay */ }
    }
    this._startDrift();
  }

  pause() { if (!this.ready) return; this.playing = false; for (const s of this.stems) s.el.pause(); this._stopDrift(); }

  stop() { this.pause(); if (this.ready) for (const s of this.stems) s.el.currentTime = 0; }

  seek(t) {
    if (!this.ready) return;
    const T = clamp(t, 0, this.duration || t);
    for (const s of this.stems) s.el.currentTime = T;
    if (this.playing) for (const s of this.stems) { s.el.playbackRate = this.speed; try { s.el.play(); } catch (_) { /* */ } }
  }

  setSpeed(v) { this.speed = v; if (this.ready) for (const s of this.stems) s.el.playbackRate = v; }

  setVolume(name, v) {
    this.stemVolumes[name] = clamp(v, 0, 1.5);
    this.applyVolumes();
  }
  setMasterVolume(v) {
    this.masterVolume = clamp(v, 0, 1.0);
    this.applyVolumes();
  }
  toggleMasterMute() {
    this.masterMuted = !this.masterMuted;
    this.applyVolumes();
    return this.masterMuted;
  }
  applyVolumes() {
    if (!this.ready) return;
    for (const s of this.stems) {
      if (this.masterMuted) {
        s.el.volume = 0;
      } else {
        const base = (this.stemVolumes[s.name] != null) ? this.stemVolumes[s.name] : 1.0;
        s.el.volume = clamp(base * this.masterVolume, 0, 1.0);
      }
    }
  }

  toggleMute(name) {
    this.muted.has(name) ? this.muted.delete(name) : this.muted.add(name);
    this.refreshMix();
  }
  toggleSolo(name) { this.solo = this.solo === name ? null : name; this.refreshMix(); }
  refreshMix() {
    if (!this.ready) return;
    for (const s of this.stems) {
      const audible = !this.muted.has(s.name) && (!this.solo || this.solo === s.name);
      s.el.muted = !audible;
    }
  }

  setLoopA(t) { this.loop.a = clamp(t, 0, this.duration); this._enableLoop(); }
  setLoopB(t) { this.loop.b = clamp(t, 0, this.duration); this._enableLoop(); }
  setLoopBarA(idx) { this.loop.barA = idx; this.loop.on = false; }
  setLoopBarB(idx) { this.loop.barB = idx; }
  _enableLoop() { if (this.loop.a >= 0 && this.loop.b > this.loop.a && !this.loop.on) this.loop.on = true; }

  _startDrift() {
    if (this._drift) clearInterval(this._drift);
    this._drift = setInterval(() => {
      if (!this.playing || !this.master) return;
      const base = this.master.el.currentTime;
      for (const s of this.stems) {
        if (s === this.master) continue;
        let rate = this.speed;
        const d = s.el.currentTime - base;
        if (Math.abs(d) > 0.045) rate = this.speed * clamp(1 - d * 0.15, 0.955, 1.045);
        if (Math.abs(s.el.playbackRate - rate) > 0.0015) s.el.playbackRate = rate;
      }
    }, 200);
  }
  _stopDrift() { if (this._drift) { clearInterval(this._drift); this._drift = null; } }

  destroy() {
    this.pause();
    for (const s of this.stems) { try { s.el.removeAttribute('src'); s.el.load(); } catch (_) { /* */ } }
    this.stems = [];
    this.ready = false;
  }
}

/* ------------------------- métronome (regénérateur) ----------------------- */
class Metronome {
  constructor(engine, getBeats, getOffset) {
    this.engine = engine;
    this.getBeats = getBeats;
    this.getOffset = getOffset;
    this.ctx = null;
    this.timer = null;
    this.enabled = false;
    this.scheduled = new Set();
    this.volume = 0.7;
  }

  setVolume(v) { this.volume = Math.max(0, Math.min(1, v)); }

  ensure() {
    if (!this.ctx) { const AC = window.AudioContext || window.webkitAudioContext; if (AC) this.ctx = new AC(); }
    if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume();
    return this.ctx;
  }

  toggle() { this.enabled ? this.stop() : this.start(); return this.enabled; }

  start() {
    this.stop();
    this.enabled = true;
    const ctx = this.ensure();
    if (!ctx) return false;
    this.scheduled.clear();
    this.timer = setInterval(() => {
      if (!this.engine.playing) return;
      const base = this.engine.currentTime;
      const horizon = base + 0.15 * this.engine.speed;
      const beats = this.getBeats() || [];
      const offset = this.getOffset() || 0;
      for (let i = 0; i < beats.length; i++) {
        const t = beats[i];
        if (t >= base - 0.02 && t <= horizon && !this.scheduled.has(i)) {
          this.scheduled.add(i);
          const wall = Math.max(0, (t - base) / this.engine.speed);
          const isAccent = ((i - offset) % 4 + 4) % 4 === 0;
          this.click(ctx.currentTime + wall, isAccent);
        }
      }
      if (this.scheduled.size > 200) {
        this.scheduled.forEach(idx => { if (beats[idx] < base - 2) this.scheduled.delete(idx); });
      }
    }, 25);
    return true;
  }

  stop() {
    this.enabled = false;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    this.scheduled.clear();
  }

  click(at, accent) {
    const ctx = this.ctx; if (!ctx) return;
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = 'square';
    o.frequency.value = accent ? 1200 : 750;
    const vol = this.volume ?? 0.7;
    const peak = (accent ? 0.35 : 0.18) * vol;
    g.gain.setValueAtTime(Math.max(0.0001, peak), at);
    g.gain.exponentialRampToValueAtTime(0.0001, at + 0.035);
    o.connect(g); g.connect(ctx.destination);
    o.start(at); o.stop(at + 0.04);
  }
}

/* -------------------------------- vue joueur ------------------------------ */
const App = { track: null, engine: null, metro: null };

function showView(name) {
  $$('.view').forEach(v => v.classList.toggle('hidden', v.id !== `view-${name}`));
}

let lastCurChord = null;
let lastNxtChord = null;
let lastBarEl = null;
let dragSeek = false;
let transposeDelta = 0;
let preferFlats = false;
function getActiveChord(chordName) {
  return transposeChord(chordName, transposeDelta, preferFlats);
}

function renderDualNeck(curChord, nxtChord) {
  const hostCur = $('#diag-current');
  const hostNxt = $('#diag-next');
  const cardCur = $('#card-current');
  const stage = $('#chord-stage');
  if (!hostCur || !hostNxt) return;
  if (curChord === lastCurChord && nxtChord === lastNxtChord) return;
  // Si l'accord a changé : fondu doux et mise à jour
  if (lastCurChord !== null && curChord !== lastCurChord && stage) {
    stage.classList.add('chord-transitioning');
    setTimeout(() => {
      hostCur.innerHTML = chordDiagramHTML(curChord);
      hostNxt.innerHTML = nxtChord ? chordDiagramHTML(nxtChord) : '<div class="noc">Fin du morceau</div>';
      stage.classList.remove('chord-transitioning');
      if (cardCur) {
        cardCur.classList.remove('pulse');
        void cardCur.offsetWidth; // force le reflow pour relancer l'animation
        cardCur.classList.add('pulse');
      }
    }, 140);
  } else {
    hostCur.innerHTML = chordDiagramHTML(curChord);
    hostNxt.innerHTML = nxtChord ? chordDiagramHTML(nxtChord) : '<div class="noc">Fin du morceau</div>';
  }
  lastCurChord = curChord;
  lastNxtChord = nxtChord;
}

function findActiveBar(t) {
  const bars = (App.track.chords && App.track.chords.bars) || [];
  for (let i = 0; i < bars.length; i++) { if (t >= bars[i].start && t < bars[i].end) return { i, bar: bars[i] }; }
  if (bars.length && t >= bars[bars.length - 1].end) return { i: bars.length - 1, bar: bars[bars.length - 1] };
  return null;
}

function updateLoopUI() {
  const L = App.engine.loop;
  $('#loop-info').textContent = (L.on && L.barA != null && L.barB != null)
    ? `mes ${L.barA + 1}–${L.barB + 1}` : (L.on ? 'A→B' : '—');
  $$('.bar.loop, .bar.loop-a, .bar.loop-b').forEach(b => b.classList.remove('loop', 'loop-a', 'loop-b'));
  $$('.bar').forEach((cell, i) => {
    if (L.on && L.barA != null && L.barB != null) {
      if (i === L.barA) cell.classList.add('loop-a');
      else if (i === L.barB) cell.classList.add('loop-b');
      else if (i > L.barA && i < L.barB) cell.classList.add('loop');
    }
  });
}

let lastActiveMeasure = null; // suivi automatique de la mesure active (scroll)

function tick() {
  const engine = App.engine;
  if (engine && engine.ready) {
    const t = engine.currentTime;

    // boucle A/B
    if (engine.playing && engine.loop.on && engine.loop.b > engine.loop.a) {
      if (t >= engine.loop.b || t < engine.loop.a) engine.seek(engine.loop.a);
    }

    // temps & seekbar
    if (!dragSeek) {
      $('#t-cur').textContent = fmtTime(t);
      $('#seek').value = engine.duration ? (t / engine.duration) * 100 : 0;
    }

    // grille : mesure + temps actifs
    const hit = findActiveBar(t);
    $$('.bar.active').forEach(b => b.classList.remove('active'));
    $$('.beat.hot').forEach(b => b.classList.remove('hot'));
    if (hit) {
      const cell = $('.bar[data-m="' + hit.bar.measure + '"]');
      if (cell) {
        cell.classList.add('active');
        if (lastActiveMeasure !== hit.bar.measure) {
          lastActiveMeasure = hit.bar.measure;
          const vp = $('#grid-viewport');
          if (vp) {
            const cellTop = cell.offsetTop;
            const cellBottom = cellTop + cell.offsetHeight;
            const vpScroll = vp.scrollTop;
            const vpH = vp.clientHeight;

            // Anticipation Look-Ahead : si la cellule active entre dans le dernier tiers (la 4e ligne visible)
            // on fait monter la vue d'une ligne pour que la ligne suivante soit déjà visible en bas !
            if (cellBottom > vpScroll + vpH - 80) {
              vp.scrollTo({ top: cellTop - 180, behavior: 'smooth' });
            } else if (cellTop < vpScroll) {
              vp.scrollTo({ top: cellTop - 10, behavior: 'smooth' });
            }
          }
        }
      }
      const bts = hit.bar.times;
      const idx = bts.reduce((a, x, i) => (t >= x ? i : a), -1);
      if (cell && idx >= 0) {
        const beats = [...cell.querySelectorAll('.beat')];
        if (beats[idx]) beats[idx].classList.add('hot');
      }
      const curChord = hit.bar.chord;
      const allBars = (App.track && App.track.chords && App.track.chords.bars) || [];
      const nextBar = allBars[hit.i + 1];
      const nxtChord = nextBar ? nextBar.chord : null;
      renderDualNeck(getActiveChord(curChord), getActiveChord(nxtChord));
    }
  }
  requestAnimationFrame(tick);
}

function getSectionStyle(tagName) {
  const norm = String(tagName || '').toLowerCase();
  if (norm.includes('intro')) return { cls: 'tag-intro', lineCls: 'line-start-intro', icon: '🟣' };
  if (norm.includes('couplet') || norm.includes('verse')) return { cls: 'tag-couplet', lineCls: 'line-start-couplet', icon: '🔵' };
  if (norm.includes('refrain') || norm.includes('chorus')) return { cls: 'tag-refrain', lineCls: 'line-start-refrain', icon: '🟠' };
  if (norm.includes('pont') || norm.includes('bridge')) return { cls: 'tag-pont', lineCls: 'line-start-pont', icon: '🟢' };
  if (norm.includes('solo')) return { cls: 'tag-solo', lineCls: 'line-start-solo', icon: '🎸' };
  if (norm.includes('outro')) return { cls: 'tag-outro', lineCls: 'line-start-outro', icon: '🌸' };
  return { cls: 'tag-default', lineCls: 'line-start-default', icon: '🔷' };
}

function buildPlayer(track) {
  const host = $('#view-player');
  const chords = track.chords || { bpm: track.bpm || 120, bars: [] };
  host.innerHTML = `
    <div class="px-head">
      <button class="btn icon" id="btn-back" title="Retour à la bibliothèque">←</button>
      <div class="px-title">
        <h2>${esc(track.title)}</h2>
        <div class="sub">
          <span>${esc(track.artist || '—')}</span>
          <span>•</span><span>${fmtTime(track.duration || 0)}</span>
          <span>•</span><span id="bpm-chip">${(chords.bpm || 0).toFixed(0)} BPM</span>
          <span>•</span><span>${chords.bars.length} mesures</span>
        </div>
      </div>
      <div class="px-actions">
        <!-- Transpose group existant -->
        <div class="transpose-group" title="Transposition des accords (demi-tons)">
          <span class="tr-lbl">Transpo</span>
          <button class="t-btn-tr" id="btn-trans-down" title="Descendre d'un demi-ton">-1</button>
          <span class="tr-val" id="trans-val">0</span>
          <button class="t-btn-tr" id="btn-trans-up" title="Monter d'un demi-ton">+1</button>
          <button class="t-btn-enh" id="btn-trans-enh" title="Basculer dièses (#) / bémols (♭)">♭/#</button>
          <button class="t-btn-reset" id="btn-trans-reset" title="Rétablir tonalité d'origine">↺</button>
        </div>
        <!-- SOLO & TABLATURE MIS EN PAUSE
        <button class="btn ghost" id="btn-midi">MIDI <a id="midi-link" hidden></a></button>
        <button class="btn ghost" id="btn-tab">Tablature</button>
        -->
        <button class="btn danger" id="btn-del">Supprimer</button>
      </div>
    </div>

    <div class="transport" id="transport">
      <button class="t-btn primary-cta" id="btn-play" title="Lecture / pause">▶</button>
      <button class="t-btn" id="btn-stop" title="Stop">■</button>
      <span class="t-time"><b id="t-cur">0:00</b> / <span id="t-tot">${fmtTime(track.duration)}</span></span>
      <input type="range" class="seekbar" id="seek" min="0" max="100" step="0.01" value="0" />
      <select id="speed" class="speed" title="Vitesse sans changement de hauteur">
        <option value="0.5">0.5×</option>
        <option value="0.75">0.75×</option>
        <option value="0.9">0.9×</option>
        <option value="1" selected>1.0×</option>
      </select>
      <div class="tr-group">
        <button class="t-btn" id="btn-loop" title="Boucle A/B">⤾</button>
        <button class="t-btn" id="btn-setA" title="Définir A = position">A</button>
        <button class="t-btn" id="btn-setB" title="Définir B = position">B</button>
        <span class="loop-info" id="loop-info">—</span>
      </div>
<div class="metro-group" title="Volume du métronome">
        <button class="t-btn" id="btn-metro" title="Activer / désactiver le métronome">♩</button>
        <input type="range" class="metro-vol" id="metro-vol" min="0" max="100" value="70" title="Volume du métronome" />
      </div>
      <button class="t-btn" id="btn-bar-shift" title="Décaler le 1er temps (anacrouse / aligner le temps 1)">⇄ T1</button>
      <span class="lbl" style="opacity:.6">♩</span>
    </div>

    <div class="mobile-tabs" id="mobile-tabs">
      <button class="mobile-tab-btn active" data-tab="chords">🎸 Accords &amp; Grille</button>
      <button class="mobile-tab-btn" data-tab="mixer">🎛️ Mixeur Stems</button>
    </div>
    <div class="px-grid">
      <aside class="mixer">
        <div class="mixer-title">Mixeur / stems</div>
        <div id="channels"></div>
        <div class="ch master-ch">
          <span class="color"></span>
          <div class="meta">
            <div class="name">Volume Général</div>
            <div class="lvl" id="lvl-master">100 %</div>
          </div>
          <input type="range" class="fader" id="fd-master" min="0" max="100" value="100" title="Volume général du morceau" />
          <button class="ms mute" id="ms-mute-master" title="Couper tout le son">M</button>
        </div>
      </aside>
      <aside class="chord-now">
        <div class="chord-stage" id="chord-stage">
          <div class="chord-card current" id="card-current">
            <span class="card-badge badge-now">Accord actuel</span>
            <div id="diag-current"><div class="noc">Chargement…</div></div>
          </div>
          <div class="chord-card next" id="card-next">
            <span class="card-badge badge-next">Suivant ➔</span>
            <div id="diag-next"><div class="noc">—</div></div>
          </div>
        </div>
      </aside>
    </div>

    <div class="grid-wrap">
      <div class="grid-title">
        <span>Grille d'accords — Vue 16 mesures (anticipée)</span>
        <div class="grid-cols-toggle">
          <button class="btn-reset-grid" id="btn-reset-grid" title="Annuler tous les sauts de ligne et revenir à la grille 4x4 standard">↺ Réinitialiser</button>
          <button id="btn-col-4" class="active" title="4 mesures par ligne">4 / ligne</button>
          <button id="btn-col-8" title="8 mesures par ligne">8 / ligne</button>
        </div>
      </div>
      <div class="section-nav" id="section-nav"></div>
      <div class="grid-viewport" id="grid-viewport">
        <div class="chord-grid" id="grid"></div>
      </div>
    </div>
  `;

  const engine = new AudioEngine(track);
  App.engine = engine;
  const rawBars = (track.chords && track.chords.bars) || [];
  const allBeats = rawBars.flatMap(b => b.times);
  const allSigs = rawBars.flatMap(b => b.sig);
  let barOffset = track.bar_offset || 0;
  const lineBreaks = new Set((track.line_breaks || []).map(Number));
  const sections = Object.assign({}, track.sections || {});
  // Réinit la transposition à l'ouverture d'un morceau
  transposeDelta = 0;
  preferFlats = false;
  function updateSectionNav() {
    const nav = $('#section-nav');
    if (!nav) return;
    const entries = Object.entries(sections).sort((a, b) => (+a[0]) - (+b[0]));
    nav.innerHTML = entries.map(([m, tag]) => {
      const style = getSectionStyle(tag);
      return `<button class="section-pill ${style.cls}" data-m="${m}">${style.icon} ${tag} (m.${m})</button>`;
    }).join('');
    nav.querySelectorAll('.section-pill').forEach(btn => {
      btn.onclick = () => {
        const m = +btn.dataset.m;
        const bar = chords.bars.find(b => b.measure === m);
        if (bar) {
          engine.seek(bar.start);
          const el = $('.bar[data-m="' + m + '"]');
          el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      };
    });
  }
  function populateGrid(chordsData) {
    const grid = $('#grid');
    if (!grid) return;
    updateSectionNav();

    grid.innerHTML = chordsData.bars.map((b) => {
      const mInt = +b.measure;
      const hasBreak = lineBreaks.has(mInt);
      const secTag = sections[String(mInt)];
      const style = secTag ? getSectionStyle(secTag) : null;
      return `
        <div class="bar ${b.chord === 'N' ? 'empty' : ''} ${hasBreak ? 'line-break' : ''} ${style ? style.lineCls : ''}" data-m="${b.measure}">
          <div class="bar-head">
            <span class="m">${b.measure}</span>
            <span class="c">${esc(getActiveChord(b.chord))}</span>
            ${secTag ? `<span class="sec-chip ${style.cls}">${style.icon} ${esc(secTag.toUpperCase())}</span>` : ''}
            <span class="b-tools">
              <button data-act="tag" title="Étiqueter (Intro, Couplet, Refrain...)">🏷</button>
              <button data-act="break" class="${hasBreak ? 'on' : ''}" title="${hasBreak ? 'Annuler le saut' : 'Saut de ligne'}">${hasBreak ? '✕' : '↵'}</button>
              <button data-act="A" title="Boucle : début">A</button>
              <button data-act="B" title="Boucle : fin">B</button>
            </span>
          </div>
          <div class="beats">${b.times.map(() => '<span class="beat"></span>').join('')}</div>
        </div>`;
    }).join('');
  }
  function saveStructure() {
    fetch(`/api/tracks/${track.id}/structure`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        line_breaks: Array.from(lineBreaks),
        sections: sections
      })
    }).catch(console.error);
  }
  function showTagPopover(targetBtn, mNum) {
    $$('.tag-popover').forEach(p => p.remove());
    const pop = document.createElement('div');
    pop.className = 'tag-popover';
    const tags = ['Intro', 'Couplet', 'Refrain', 'Pont', 'Solo', 'Outro', 'Supprimer'];
    pop.innerHTML = tags.map(t => `<button data-tag="${t}">${t}</button>`).join('');
    const r = targetBtn.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 150)) + 'px';
    pop.style.top = (r.bottom + 6) + 'px';
    pop.onclick = (e) => {
      const t = e.target.dataset.tag;
      if (!t) return;
      if (t === 'Supprimer') {
        delete sections[String(mNum)];
        lineBreaks.delete(+mNum);
      } else {
        sections[String(mNum)] = t;
        lineBreaks.add(+mNum);
      }
      pop.remove();
      populateGrid(chords);
      saveStructure();
    };
    document.body.appendChild(pop);
    setTimeout(() => {
      window.addEventListener('click', (ev) => { if (!pop.contains(ev.target)) pop.remove(); }, { once: true });
    }, 50);
  }
  function dominantChord(sigs) {
    const real = sigs.filter(s => s && s !== 'N');
    if (!real.length) return 'N';
    const counts = {};
    real.forEach(c => counts[c] = (counts[c] || 0) + 1);
    return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
  }
  function computeBars(offset) {
    const bars = [];
    let m = 1;
    const o = ((offset % 4) + 4) % 4;
    if (o > 0 && allBeats.length >= o) {
      const pBeats = allBeats.slice(0, o);
      const pSigs = allSigs.slice(0, o);
      bars.push({
        measure: m++,
        start: pBeats[0],
        end: allBeats[o],
        chord: dominantChord(pSigs),
        times: pBeats,
        sig: pSigs
      });
    }
    for (let i = o; i < allBeats.length; i += 4) {
      const segB = allBeats.slice(i, i + 4);
      const segS = allSigs.slice(i, i + 4);
      if (!segB.length) continue;
      const end = (i + 4 < allBeats.length) ? allBeats[i + 4] : segB[segB.length - 1] + 0.67;
      bars.push({
        measure: m++,
        start: segB[0],
        end: Math.round(end * 1000) / 1000,
        chord: dominantChord(segS),
        times: segB,
        sig: segS
      });
    }
    return bars;
  }
  if (barOffset > 0) {
    chords.bars = computeBars(barOffset);
    App.track.chords = chords;
  }
  $('#btn-bar-shift').textContent = `⇄ T${barOffset + 1}`;
  App.metro = new Metronome(engine, () => allBeats, () => barOffset);
  lastCurChord = null;
  lastNxtChord = null;
  lastActiveMeasure = null;

  // mixeur
  const chBox = $('#channels');
  chBox.innerHTML = track.stems.map(name => `
    <div class="ch" id="ch-${name}" style="--cc:${stemColor(name)}">
      <span class="color" style="background:${stemColor(name)}"></span>
      <div class="meta">
        <div class="name">${esc(stemLabel(name))}</div>
        <div class="lvl" id="lvl-${name}">100 %</div>
      </div>
      <input type="range" class="fader" id="fd-${name}" min="0" max="150" value="100" title="Volume" />
      <button class="ms mute" id="ms-mute-${name}" title="Mute">M</button>
      <button class="ms solo" id="ms-solo-${name}" title="Solo">S</button>
    </div>`).join('');
  track.stems.forEach(name => {
    $('#fd-' + name).addEventListener('input', (e) => {
      engine.setVolume(name, e.target.value / 100);
      $('#lvl-' + name).textContent = `${Math.round((e.target.value / 100) * 100)} %`;
    });
    $('#ms-mute-' + name).addEventListener('click', () => {
      engine.toggleMute(name);
      $('#ms-mute-' + name).classList.toggle('on', engine.muted.has(name));
      $('#ch-' + name).classList.toggle('muted', engine.muted.has(name));
    });
    $('#ms-solo-' + name).addEventListener('click', () => {
      engine.toggleSolo(name);
      track.stems.forEach(n => {
        $('#ch-' + n).classList.toggle('soloed', engine.solo === n);
        $('#ms-solo-' + n).classList.toggle('on', engine.solo === n);
      });
    });
  });
  $('#fd-master')?.addEventListener('input', (e) => {
    const val = e.target.value / 100;
    engine.setMasterVolume(val);
    $('#lvl-master').textContent = `${Math.round(val * 100)} %`;
  });
  $('#ms-mute-master')?.addEventListener('click', () => {
    const isMuted = engine.toggleMasterMute();
    $('#ms-mute-master').classList.toggle('on', isMuted);
    $('.master-ch').classList.toggle('muted', isMuted);
  });

  // grille
  populateGrid(chords);
  const grid = $('#grid');
  grid.addEventListener('click', (ev) => {
    const tool = ev.target.closest('button[data-act]');
    const cell = ev.target.closest('.bar');
    if (!cell) return;
    const mNum = +cell.dataset.m;
    const bar = chords.bars.find(b => b.measure === mNum);
    if (!bar) return;
    if (tool) {
      const act = tool.dataset.act;
      const mInt = +bar.measure;
      if (act === 'break') {
        if (lineBreaks.has(mInt)) {
          lineBreaks.delete(mInt);
        } else {
          lineBreaks.add(mInt);
        }
        populateGrid(chords);
        saveStructure();
        barActionFlash(tool);
        return;
      }
      if (act === 'tag') {
        showTagPopover(tool, mInt);
        return;
      }
      const i = chords.bars.indexOf(bar);
      if (act === 'A') { engine.setLoopBarA(i); engine.setLoopA(bar.start); }
      else { engine.setLoopBarB(i); engine.setLoopB(bar.end); }
      updateLoopUI();
      barActionFlash(tool);
      return;
    }
    // Clic sur une mesure : désactive la boucle et saute directement à bar.start
    engine.loop.on = false;
    $('#btn-loop')?.classList.remove('on');
    updateLoopUI();

    engine.seek(bar.start);
    if (!engine.playing) {
      engine.play();
      $('#btn-play').textContent = '⏸';
    }
  });

  // alignement du temps 1 (anacrouse / carrure)
  $('#btn-bar-shift')?.addEventListener('click', () => {
    barOffset = (barOffset + 1) % 4;
    $('#btn-bar-shift').textContent = `⇄ T${barOffset + 1}`;
    chords.bars = computeBars(barOffset);
    App.track.chords.bars = chords.bars;
    populateGrid(chords);
    updateLoopUI();
    const fd = new FormData();
    fd.append('offset', barOffset);
    fetch(`/api/tracks/${track.id}/bar-offset`, { method: 'POST', body: fd }).catch(console.error);
  });

  // onglets mobiles : bascule Mixeur / Accords
  $('#mobile-tabs')?.addEventListener('click', (e) => {
    const btn = e.target.closest('.mobile-tab-btn');
    if (!btn) return;
    $$('.mobile-tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const isMixer = btn.dataset.tab === 'mixer';
    $('.mixer').classList.toggle('tab-hidden-mobile', !isMixer);
    $('.chord-now').classList.toggle('tab-hidden-mobile', isMixer);
    $('.grid-wrap').classList.toggle('tab-hidden-mobile', isMixer);
  });
  // État initial sur mobile : mixer caché au profit des accords
  if (window.innerWidth <= 768) {
    $('.mixer')?.classList.add('tab-hidden-mobile');
  }

  // transport
  $('#btn-back')?.addEventListener('click', showHome);

  // transposition des accords
  const transVal = $('#trans-val');
  const btnTransReset = $('#btn-trans-reset');
  function updateTransposeUI() {
    if (transVal) {
      transVal.textContent = (transposeDelta > 0 ? '+' : '') + transposeDelta;
      transVal.classList.toggle('shifted', transposeDelta !== 0);
    }
    if (btnTransReset) {
      btnTransReset.style.visibility = transposeDelta === 0 ? 'hidden' : 'visible';
    }
    // Rafraîchit immédiatement la grille
    populateGrid(chords);
    // Rafraîchit immédiatement les diagrammes en cours
    const t = App.engine ? App.engine.currentTime : 0;
    const hit = findActiveBar(t);
    if (hit) {
      const cur = hit.bar.chord;
      const allBars = chords.bars || [];
      const nxt = allBars[hit.i + 1] ? allBars[hit.i + 1].chord : null;
      lastCurChord = null; // force le rafraîchissement
      renderDualNeck(getActiveChord(cur), getActiveChord(nxt));
    }
  }
  $('#btn-trans-down')?.addEventListener('click', () => {
    transposeDelta -= 1;
    // Par défaut en baissant, on privilégie souvent les bémols
    if (transposeDelta < 0 && !preferFlats) preferFlats = true;
    updateTransposeUI();
  });
  $('#btn-trans-up')?.addEventListener('click', () => {
    transposeDelta += 1;
    if (transposeDelta > 0 && preferFlats) preferFlats = false;
    updateTransposeUI();
  });
  $('#btn-trans-enh')?.addEventListener('click', () => {
    preferFlats = !preferFlats;
    updateTransposeUI();
  });
  btnTransReset?.addEventListener('click', () => {
    transposeDelta = 0;
    preferFlats = false;
    updateTransposeUI();
  });

  $('#btn-play').onclick = () => {
    if (engine.playing) { engine.pause(); $('#btn-play').textContent = '▶'; }
    else { engine.play(); $('#btn-play').textContent = '⏸'; }
  };
  $('#btn-stop').onclick = () => { engine.stop(); $('#btn-play').textContent = '▶'; $('#t-cur').textContent = '0:00'; $('#seek').value = 0; };
  $('#seek').addEventListener('input', (e) => { dragSeek = true; $('#t-cur').textContent = fmtTime(e.target.value / 100 * (engine.duration || 0)); });
  $('#seek').addEventListener('change', (e) => { engine.seek(e.target.value / 100 * (engine.duration || 0)); dragSeek = false; });
  $('#speed').onchange = (e) => engine.setSpeed(parseFloat(e.target.value));
  $('#btn-setA').onclick = () => { engine.setLoopA(engine.currentTime); updateLoopUI(); };
  $('#btn-setB').onclick = () => { engine.setLoopB(engine.currentTime); updateLoopUI(); };
  $('#btn-loop').onclick = () => {
    engine.loop.on = !engine.loop.on;
    $('#btn-loop').classList.toggle('on', engine.loop.on);
    updateLoopUI();
  };
  $('#btn-metro').onclick = () => { $('#btn-metro').classList.toggle('on', App.metro.toggle()); };
  $('#metro-vol')?.addEventListener('input', (e) => {
    App.metro.setVolume(e.target.value / 100);
  });
  $('#btn-reset-grid')?.addEventListener('click', () => {
    if (!confirm('Annuler tous les sauts de ligne et remettre la grille 4x4 d\'origine ?')) return;
    lineBreaks.clear();
    Object.keys(sections).forEach(k => delete sections[k]);
    populateGrid(chords);
    saveStructure();
  });
  $('#btn-col-4')?.addEventListener('click', () => {
    $('#grid')?.classList.remove('cols-8');
    $('#btn-col-4').classList.add('active');
    $('#btn-col-8').classList.remove('active');
  });
  $('#btn-col-8')?.addEventListener('click', () => {
    $('#grid')?.classList.add('cols-8');
    $('#btn-col-8').classList.add('active');
    $('#btn-col-4').classList.remove('active');
  });

  // actions (mise en pause solo/tablature)
  // $('#btn-midi').onclick = () => { const a = $('#midi-link'); a.href = track.files.midi; a.download = 'guitar_solo.mid'; a.click(); return false; };
  // $('#btn-tab').onclick = async () => {
  //   try {
  //     const r = await fetch(track.files.tab);
  //     const body = r.ok ? await r.text() : 'Tablature non disponible.';
  //     openModal('Tablature (solo / lead)', body, track);
  //   } catch (_) { alert('Impossible de charger la tablature.'); }
  // };
  $('#btn-del').onclick = async () => {
    if (!confirm('Supprimer définitivement ce morceau ?')) return;
    engine.destroy();
    await API.remove(track.id);
    showHome();
  };

  // chargement des stems
  engine.load().then(() => {
    $('#btn-play').disabled = false;
    $('#btn-stop').disabled = false;
    $('#t-tot').textContent = fmtTime(engine.duration);
  });
  $('#btn-play').disabled = true; $('#btn-stop').disabled = true;

  updateLoopUI();
  renderDualNeck(getActiveChord(chords.bars.length ? chords.bars[0].chord : 'N'), getActiveChord(chords.bars.length > 1 ? chords.bars[1].chord : null));
}

function barActionFlash(el) { el.style.color = 'var(--accent-2)'; setTimeout(() => { el.style.color = ''; }, 350); }

function openModal(title, text, track) {
  const veil = document.createElement('div');
  veil.className = 'modal-veil';
  veil.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <h3>${esc(title)}</h3>
        <a class="btn ghost" href="${esc(track.files.midi)}" download>⬇ MIDI</a>
        <button class="btn ghost" data-close>✕</button>
      </div>
      <div class="modal-body"><p class="dim">${esc(track.title)} — stem guitare</p><pre>${esc(text)}</pre></div>
    </div>`;
  veil.addEventListener('click', (e) => { if (e.target === veil || e.target.closest('[data-close]')) veil.remove(); });
  document.body.appendChild(veil);
}

/* --------------------------------- accueil --------------------------------- */
let currentFile = null;
let jobCtrl = null;   // fermeture active du terminal (stop polling/WS) si un job tourne

function showHome() {
  if (App.engine) { App.engine.destroy(); App.engine = null; }
  if (App.metro) { App.metro.stop(); App.metro = null; }
  showView('home');
  refreshLibrary();
}

async function refreshLibrary() {
  const lib = $('#library');
  const empty = $('#empty-lib');
  let tracks = [];
  try { tracks = await API.tracks(); } catch (_) { /* backend indisponible */ }
  $('#lib-count').textContent = `${tracks.length} morceau${tracks.length > 1 ? 'x' : ''}`;
  empty.classList.toggle('hidden', tracks.length > 0);

  lib.innerHTML = tracks.map(t => {
    const chipCls = t.status === 'ready' ? 'ready' : (t.status === 'error' ? 'error' : 'queue');
    const chipTxt = t.status === 'ready' ? 'Prêt' : (t.status === 'error' ? 'Erreur' : t.status);
    const thumb = t.thumbnail
      ? `style="background-image:url('${esc(t.thumbnail)}')"`
      : '';
    const meta = [t.artist, t.duration ? fmtTime(t.duration) : null, t.bpm ? `${t.bpm.toFixed(0)} BPM` : null]
      .filter(Boolean).join(' · ');
    return `
      <article class="card" data-id="${esc(t.id)}" data-title="${esc(t.title)}">
        <div class="thumb" ${thumb}>
          ${t.thumbnail ? '' : '<span class="fallback">🎸</span>'}
          <button class="card-del" title="Supprimer" aria-label="Supprimer ${esc(t.title)}">✕</button>
          <span class="chip ${chipCls}">${chipTxt}</span>
        </div>
        <div class="card-body">
          <h3>${esc(t.title)}</h3>
          <div class="meta">${esc(meta)}</div>
          <div class="fade">${esc((t.stems || []).length)} stems — ${esc(t.source || '')}</div>
          <button class="btn primary">Ouvrir le labo</button>
        </div>
      </article>`;
  }).join('');

  lib.querySelectorAll('article.card').forEach(card => {
    const btn = card.querySelector('.btn');
    btn.addEventListener('click', async () => {
      try { await openPlayer(card.dataset.id); }
      catch (_) { alert('Impossible d’ouvrir ce morceau (traitement incomplet ?).'); }
    });
    const del = card.querySelector('.card-del');
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      const name = card.dataset.title || 'ce morceau';
      if (!confirm(`Supprimer « ${name} » de la bibliothèque ?`)) return;
      try {
        await API.remove(card.dataset.id);
        card.remove();
        refreshLibrary();
      } catch (_) {
        alert('Suppression impossible (backend injoignable ?).');
      }
    });
  });
}

async function openPlayer(id) {
  const track = await API.track(id);
  if (track.status !== 'ready') {
    alert('Ce morceau n’est pas encore prêt.');
    return;
  }
  App.track = track;
  showView('player');
  buildPlayer(track);
}

function jobRender(s, auto) {
  const panel = $('#job-panel');
  panel.classList.remove('hidden');
  const dot = $('#job-dot');
  const isErr = s.status === 'error';
  const isRdy = s.status === 'ready';
  dot.className = 'status-dot ' + (isErr ? 'error' : (isRdy ? 'ready' : 'processing'));
  $('#job-title').textContent = isErr ? 'Traitement en échec' : (isRdy ? 'Terminé' : `Traitement… (${s.status})`);
  $('#job-pct').textContent = `${s.progress}%`;
  $('#job-bar').style.width = `${s.progress}%`;
  $('#job-id').textContent = s.id || '';
  const logEl = $('#job-logs');
  const logs = s.logs || [];
  if (logEl.children.length > logs.length) logEl.innerHTML = ''; // logs expurgés → reset
  while (logEl.children.length < logs.length) {
    const i = logEl.children.length;
    const div = document.createElement('div');
    div.className = 'term-line me' + (logs[i].startsWith('✖') ? ' err' : '');
    div.textContent = logs[i];
    logEl.appendChild(div);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function monitorJob(id, autoOpen = true) {
  const panel = $('#job-panel');
  panel.classList.remove('hidden');
  $('#job-logs').innerHTML = ''; // Remet le terminal à zéro
  jobRender({ id, status: 'queued', progress: 0, logs: ['En attente du backend…'] }, true);

  let usingWs = false;
  let finished = false;
  const finish = (st) => {
    if (finished) return;
    finished = true;
    setTimeout(() => {
      refreshLibrary();
      if (autoOpen && st === 'ready') openPlayer(id).catch(() => {});
      if (st === 'error') $('#form-hint').textContent = 'Le traitement a échoué — voir le journal ci-dessus.';
    }, 700);
  };

  const iv = setInterval(async () => {
    if (usingWs || finished) return;
    try { const s = await API.status(id); if (s && s.status !== 'unknown') jobRender(s); if (s && (s.status === 'ready' || s.status === 'error')) finish(s.status); }
    catch (_) { /* backend pas encore joignable */ }
  }, 1600);

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws = null;
  try {
    ws = new WebSocket(`${proto}://${location.host}/api/ws/${id}`);
  } catch (_) { /* ws indisponible → polling seul */ }
  if (ws) {
    ws.onopen = () => { usingWs = true; };
    ws.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data);
        if (ev.type === 'status') { jobRender(ev.status); if (ev.status && (ev.status.status === 'ready' || ev.status.status === 'error')) finish(ev.status.status); }
      } catch (_) { /* */ }
    };
    ws.onerror = () => { usingWs = false; };
    ws.onclose = () => { usingWs = false; };
  }

  // Bouton ✕ du terminal : stoppe la surveillance et masque le panneau.
  jobCtrl = () => {
    finished = true;
    clearInterval(iv);
    if (ws) { try { ws.close(); } catch (_) { /* */ } }
    panel.classList.add('hidden');
  };
}

async function init() {
  // Purge PWA : désenregistre les service workers et vide les caches pour
  // que les nouvelles ressources (boutons, scripts) soient servies sans être
  // bloquées par une ancienne version mise en cache.
  try {
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      regs.forEach(r => r.unregister());
    }
    if ('caches' in window) {
      const keys = await caches.keys();
      keys.forEach(k => caches.delete(k));
    }
  } catch (_) { /* purge secondaire */ }

  // chip device
  try {
    const h = await API.health();
    const chip = $('#device-chip');
    chip.textContent = h.device.toUpperCase();
    if (h.device !== 'cuda') chip.classList.add('cpu');
  } catch (_) { $('#device-chip').textContent = 'OFFLINE'; }

  // navigation bibliothèque
  $('#btn-nav-home')?.addEventListener('click', showHome);
  $('#btn-brand')?.addEventListener('click', showHome);

  // formulaire
  const form = $('#add-form');
  const urlInput = $('#url-input');
  const fileInput = $('#file-input');
  const submit = $('#btn-submit');
  const hint = $('#form-hint');
  $('#job-close').onclick = () => {
    if (jobCtrl) jobCtrl();
    else $('#job-panel').classList.add('hidden');
  };
  $('#btn-file').onclick = () => fileInput.click();
  fileInput.addEventListener('change', () => {
    currentFile = fileInput.files[0] || null;
    $('#file-name').textContent = currentFile ? currentFile.name : '';
    if (currentFile && urlInput.value) { urlInput.value = ''; }
    hint.textContent = currentFile ? 'Fichier sélectionné : traitement upload.' : '';
  });
  urlInput.addEventListener('input', () => hint.textContent = '');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    hint.textContent = ''; hint.classList.remove('err');
    const fd = new FormData();
    if (urlInput.value) fd.append('url', urlInput.value.trim());
    else if (currentFile) fd.append('file', currentFile);
    else { hint.textContent = 'Renseignez une URL YouTube ou choisissez un fichier audio.'; return; }

    submit.disabled = true;
    try {
      const res = await API.process(fd);
      $('#url-input').value = '';
      currentFile = null; fileInput.value = ''; $('#file-name').textContent = '';
      if (res.duplicate && res.status === 'ready') {
        hint.textContent = 'Déjà traité — ouverture du labo.';
        refreshLibrary();
        openPlayer(res.id).catch(() => {});
      } else {
        monitorJob(res.id);
      }
    } catch (err) {
      hint.classList.add('err');
      hint.textContent = err.message || 'Échec de l’envoi.';
    } finally {
      submit.disabled = false;
    }
  });

  await refreshLibrary();
  tick();
}

/* raccourcis clavier */
window.addEventListener('keydown', (e) => {
  if (!App.track || !$('#view-player') || $('#view-player').classList.contains('hidden')) return;
  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  const eng = App.engine;
  if (!eng || !eng.ready) return;
  if (e.code === 'Space') {
    e.preventDefault();
    if (eng.playing) { eng.pause(); $('#btn-play').textContent = '▶'; }
    else { eng.play(); $('#btn-play').textContent = '⏸'; }
  } else if (e.key === 'ArrowRight') { eng.seek(eng.currentTime + 5); }
  else if (e.key === 'ArrowLeft') { eng.seek(eng.currentTime - 5); }
  else if (e.key.toLowerCase() === 's') { eng.stop(); $('#btn-play').textContent = '▶'; }
});

App.goHome = showHome;
App.openPlayer = openPlayer;
window.App = App;
window.addEventListener('DOMContentLoaded', init);