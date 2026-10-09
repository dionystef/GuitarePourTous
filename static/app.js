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

/* ---------------------------------------------------------------------------
   Nettoyage des titres : retire les mentions parasites copiées depuis les
   plateformes de streaming (« [Official Video] », « (Lyrics) », « [Clip
   Officiel] », « (Official Video Remastered) », …) afin d'afficher un titre
   net dans la bibliothèque.

   On cible les blocs [..] ou (..) dont le contenu contient au moins un mot
   parasite, puis on nettoie les séparateurs laissés aux extrémités (tiret,
   pipe, puce) et on normalise les espaces multiples créés par la suppression.
   --------------------------------------------------------------------------- */
// Liste commune des mots parasites, réutilisée par les deux expressions
// ci-dessous (blocs [..]/[()] et fins de titre) pour éviter toute duplication.
const TITLE_NOISE_WORDS = 'official|clip|video|lyric|lyrics|audio|remaster|mv|music|hd|4k|1080p|720p|karaoke|vietsub|subtitle|paroles|edit|version|dance|mix';
// Mots courts/ambigus nécessitant une frontière de mot : sans elle, « Mixing »,
// « HDTV », « Musical », « Editor », « MVG » seraient traités à tort comme parasites.
const TITLE_NOISE_BOUNDARY = new Set(['mv', 'music', 'hd', 'mix', 'edit']);
// « edit » tolère les formes fléchies « edited » / « editing » tout en restant
// borné (un « Editor » reste non parasite) — pattern partagé par les deux variantes.
const TITLE_NOISE_EDIT = '\\bedit(?:ed|ing)?\\b';
// Fragment d'alternance (partagé) : borne les mots ambigus, laisse passer les
// formes fléchies de « edit ».
const titleNoiseToken = (w) =>
  (w === 'edit' ? TITLE_NOISE_EDIT : (TITLE_NOISE_BOUNDARY.has(w) ? `\\b${w}\\b` : w));
// Alternance pour les blocs [..]/(..) : frontière de mot sur les mots ambigus.
const TITLE_NOISE_ALT = TITLE_NOISE_WORDS.split('|').map(titleNoiseToken).join('|');
const TITLE_NOISE_RE = [
  new RegExp(`\\[[^\\]]*(?:${TITLE_NOISE_ALT})[^\\]]*\\]`, 'gi'),
  new RegExp(`\\([^)]*(?:${TITLE_NOISE_ALT})[^)]*\\)`, 'gi'),
];
// Variante pour les fins de titre : les mots ambigus restent bornés (pas de
// suffixe), les autres acceptent un suffixe fréquent (« Remastered », « Lyrics »).
const titleNoiseTrailToken = (w) =>
  (w === 'edit' ? TITLE_NOISE_EDIT : (TITLE_NOISE_BOUNDARY.has(w) ? `\\b${w}\\b` : `${w}(?:s|es|ed|ing|e)?`));
const TITLE_TRAIL_ALT = TITLE_NOISE_WORDS.split('|').map(titleNoiseTrailToken).join('|');
// Parasites en fin de titre (sans parenthèses) : « - Official Video », « | Lyrics »…
const TITLE_TRAIL_RE = new RegExp(`\\s*[|·–—-]\\s*(?:${TITLE_TRAIL_ALT})(?:\\s+(?:${TITLE_TRAIL_ALT}))*\\s*$`, 'gi');
function cleanTitle(title) {
  if (!title) return 'Titre sans nom';
  let out = String(title);
  // Supprime d'abord les blocs [..] / (..) contenant un mot parasite.
  TITLE_NOISE_RE.forEach((re) => { out = out.replace(re, ''); });
  // Supprime ensuite les parasites non parenthésés en fin de titre.
  out = out.replace(TITLE_TRAIL_RE, '');
  // Séparateurs parasites restés seuls en début/fin (tiret, pipe, puce…).
  out = out.replace(/^\s*[|·–—-]\s*/, '').replace(/\s*[|·–—-]\s*$/, '');
  out = out.replace(/\s+/g, ' ').trim();
  // Garde : ne jamais renvoyer un titre vide (carte/label sans texte). On
  // retombe sur le titre brut normalisé, sinon sur un libellé neutre.
  if (!out) {
    const raw = String(title).replace(/\s+/g, ' ').trim();
    return raw || 'Titre sans nom';
  }
  return out;
}

const API = {
  async json(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      const e = new Error((await r.text().catch(() => '')) || r.statusText);
      // Porte le code HTTP : permet aux appelants (ex. suivi d'un morceau
      // supprimé → 404) de discriminer la cause avant de choisir la réaction.
      e.status = r.status;
      throw e;
    }
    return r.json();
  },
  health:   () => API.json('/api/health'),
  tracks:   () => API.json('/api/tracks'),
  track:    (id) => API.json(`/api/tracks/${id}`),
  status:   (id) => API.json(`/api/status/${id}`),
  remove:   (id) => API.json(`/api/tracks/${id}`, { method: 'DELETE' }),
  process:  (body) => API.json('/api/process', { method: 'POST', body }),
  activeJobs: () => API.json('/api/jobs/active'),
  // Métadonnées d'un morceau (titre / artiste / dossier) — PATCH au corps JSON.
  updateTrackMeta: (id, data) => API.json(`/api/tracks/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }),
  // Dossiers virtuels de la bibliothèque.
  getFolders:   () => API.json('/api/folders'),
  createFolder: (name) => {
    const fd = new FormData();
    fd.append('name', name);
    return API.json('/api/folders', { method: 'POST', body: fd });
  },
  deleteFolder: (name) =>
    API.json(`/api/folders/${encodeURIComponent(name)}`, { method: 'DELETE' }),
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
    this.stems = [];          // [{ name, buffer, gain, src }]
    this.master = null;       // référence du 1er stem (compat)
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
    this.ctx = null;
    this.masterGain = null;
    this._startOffset = 0;    // temps média au début du segment courant
    this._startCtx = 0;       // ctx.currentTime au début du segment courant
    this._stopping = false;
  }

  get currentTime() {
    if (!this.ready || !this.ctx) return this._startOffset || 0;
    if (!this.playing) return this._startOffset;
    return this._startOffset + (this.ctx.currentTime - this._startCtx) * this.speed;
  }

  async load() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { this.ready = true; return; }
    try { this.ctx = new AC(); } catch (e) { this.ctx = null; this.ready = true; return; }
    this.masterGain = this.ctx.createGain();
    this.masterGain.connect(this.ctx.destination);
    this.masterGain.gain.value = this.masterVolume;

    const results = await Promise.all(this.track.stems.map(name =>
      this._decode(name).catch(err => { console.error('decode', name, err); return null; })
    ));
    this.stems = results.filter(Boolean);
    const ds = this.stems.map(s => s.buffer.duration).filter(Boolean);
    if (ds.length) this.duration = Math.max(...ds);
    this.master = this.stems[0] || null;
    this.ready = true;
    console.log("AudioEngine prêt");
    this.refreshMix();
    this.applyVolumes();
  }

  async _decode(name) {
    const res = await fetch(`/data/${this.track.id}/stems/${name}.mp3`);
    const arr = await res.arrayBuffer();
    const buffer = await this.ctx.decodeAudioData(arr);
    const gain = this.ctx.createGain();
    gain.connect(this.masterGain);
    return { name, buffer, gain, src: null };
  }

  _resume() { if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume().catch(() => {}); }

  play() {
    if (!this.ready || this.playing || !this.stems.length) return;
    this._resume();
    this.playing = true;
    this._spawn();
  }

  _spawn() {
    const delay = 0.015;
    this._startCtx = this.ctx.currentTime + delay;
    this._stopping = true;
    for (const s of this.stems) {
      if (s.src) { try { s.src.stop(); } catch (_) {} }
      const src = this.ctx.createBufferSource();
      src.buffer = s.buffer;
      src.playbackRate.value = this.speed;
      // Préservation de la hauteur si l'implémentation le propose (sinon le ralenti
      // redeviendra "chipmunk" — compromis dû au backend WebAudio).
      try { src.preservesPitch = true; src.detune.value = 0; } catch (_) { /* non supporté */ }
      src.connect(s.gain);
      const offset = Math.max(0, Math.min(this._startOffset, s.buffer.duration - 0.001));
      src.start(this._startCtx, offset);
      src.onended = () => {
        // Ignore les sources devenues obsolètes (stop() asynchrone lors d'un seek/pause).
        if (this._stopping || s.src !== src) return;
        this.playing = false;
        this._startOffset = this.duration;
      };
      s.src = src;
    }
    this._stopping = false;
  }

  pause() {
    if (!this.ready || !this.playing) return;
    this._startOffset = this.currentTime;
    this._stopSources();
    this.playing = false;
  }

  stop() {
    this._startOffset = 0;
    this._stopSources();
    this.playing = false;
  }

  seek(t) {
    if (!this.ready) return;
    const T = clamp(t, 0, this.duration || t);
    this._startOffset = T;
    if (this.playing) this._spawn();
  }

  setSpeed(v) {
    this.speed = v;
    for (const s of this.stems) if (s.src) s.src.playbackRate.value = v;
  }

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

  _effectiveGain(name) {
    if (this.masterMuted) return 0;
    if (this.muted.has(name)) return 0;
    if (this.solo && this.solo !== name) return 0;
    const base = (this.stemVolumes[name] != null) ? this.stemVolumes[name] : 1.0;
    return clamp(base * this.masterVolume, 0, 1.0);
  }
  applyVolumes() {
    this._applyGains();
  }

  toggleMute(name) {
    this.muted.has(name) ? this.muted.delete(name) : this.muted.add(name);
    this.refreshMix();
  }
  toggleSolo(name) { this.solo = this.solo === name ? null : name; this.refreshMix(); }
  refreshMix() {
    this._applyGains();
  }

  _applyGains() {
    if (!this.ready || !this.ctx) return;
    for (const s of this.stems) {
      s.gain.gain.setTargetAtTime(this._effectiveGain(s.name), this.ctx.currentTime, 0.012);
    }
  }

  setLoopA(t) { this.loop.a = clamp(t, 0, this.duration); this._enableLoop(); }
  setLoopB(t) { this.loop.b = clamp(t, 0, this.duration); this._enableLoop(); }
  setLoopBarA(idx) { this.loop.barA = idx; this.loop.on = false; }
  setLoopBarB(idx) { this.loop.barB = idx; }
  _enableLoop() { if (this.loop.a >= 0 && this.loop.b > this.loop.a && !this.loop.on) this.loop.on = true; }

  _stopSources() {
    this._stopping = true;
    for (const s of this.stems) { if (s.src) { try { s.src.stop(); } catch (_) {} } s.src = null; }
    this._stopping = false;
  }

  destroy() {
    this._stopSources();
    this.playing = false;
    if (this.ctx) { try { this.ctx.close(); } catch (_) { /* */ } }
    this.stems = [];
    this.ctx = null;
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
// Identifiants internes des onglets système (Tous / Non classés). On les
// préfixe pour ne JAMAIS dépendre d'une égalité avec une chaîne utilisateur :
// un dossier nommé « all » ou « unclassified » (hérité d'une ancienne version
// ou créé hors API) ne doit pas collisionner avec la logique de filtre.
const FOLDER_ALL = '__all__';
const FOLDER_UNCLASSIFIED = '__none__';

// `activeFolder` pilote le filtre de la bibliothèque : FOLDER_ALL (Tous),
// FOLDER_UNCLASSIFIED (Non classés) ou un nom de dossier personnalisé.
const App = { track: null, engine: null, metro: null, activeFolder: FOLDER_ALL, tracks: [], folders: [] };

function showView(name) {
  $$('.view').forEach(v => v.classList.toggle('hidden', v.id !== `view-${name}`));
}

let lastCurChord = null;
let lastNxtChord = null;
let lastBarEl = null;
let dragSeek = false;
let transposeDelta = 0;
let preferFlats = false;
let lastLyricIndex = -2;
let lastIsInstrumental = false;
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
    updateLyricsUI(t);
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

function cleanLyric(text) {
  if (!text) return '';
  return text
    .replace(/^["'—\-\s]+|["'—\-\s]+$/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function updateLyricsUI(t) {
  const pane = $('#vis-pane-lyrics');
  if (!pane || pane.classList.contains('hidden')) return;
  const segments = App.lyricsSegments || [];
  if (!segments.length) return;
  // Recherche du segment en cours ou précédent
  let idx = segments.findIndex(s => t >= s.start && t < s.end);
  if (idx === -1) {
    idx = segments.reduce((acc, s, i) => (t >= s.start ? i : acc), -1);
  }
  let isInstrumental = false;
  let curText = '';
  let prevText = '';
  let nextText = '';
  if (idx === -1) {
    // Avant la première phrase chantée
    isInstrumental = true;
    curText = '♪ Intro instrumentale ♪';
    prevText = '';
    nextText = segments[0] ? segments[0].text : '';
  } else {
    const curSeg = segments[idx];
    const nextSeg = idx + 1 < segments.length ? segments[idx + 1] : null;
    // Pause instrumentale entre 2 phrases (silence > 3s)
    if (nextSeg && t > curSeg.end + 2 && (nextSeg.start - t > 2)) {
      isInstrumental = true;
      curText = '♪ Instrumental ♪';
      prevText = curSeg.text;
      nextText = nextSeg.text;
    } else {
      curText = curSeg.text;
      prevText = idx > 0 ? segments[idx - 1].text : '';
      nextText = nextSeg ? nextSeg.text : '';
    }
  }
  prevText = cleanLyric(prevText);
  curText = isInstrumental ? curText : cleanLyric(curText);
  nextText = cleanLyric(nextText);
  if (idx !== lastLyricIndex || isInstrumental !== lastIsInstrumental) {
    lastLyricIndex = idx;
    lastIsInstrumental = isInstrumental;
    const elPrev = $('#lyric-prev');
    const elCur = $('#lyric-cur');
    const elNext = $('#lyric-next');
    if (elPrev) elPrev.textContent = prevText.trim();
    if (elCur) {
      elCur.textContent = curText.trim() || '…';
      elCur.classList.toggle('instrumental', isInstrumental);
    }
    if (elNext) elNext.textContent = nextText.trim();
  }
}

function bindAccordion(panelId, headId, chevronId, storageKey) {
  const panel = $(panelId);
  const head = $(headId);
  const chevron = $(chevronId);
  if (!panel || !head) return;
  function apply(collapsed) {
    panel.classList.toggle('collapsed', collapsed);
    if (chevron) chevron.textContent = collapsed ? '▸' : '▾';
    localStorage.setItem(storageKey, collapsed ? 'true' : 'false');
  }
  // Rétablir l'état persisté
  const isCollapsed = localStorage.getItem(storageKey) === 'true';
  apply(isCollapsed);
  head.addEventListener('click', (e) => {
    // Ne pas replier si on clique sur les onglets (accords/paroles) ou les boutons 4/8 de la grille
    if (e.target.closest('#vis-tabs') || e.target.closest('#grid-tools')) return;
    apply(!panel.classList.contains('collapsed'));
  });
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
          <span class="sub-artist">${esc(track.artist || '—')}</span>
          <span class="sub-sep">•</span><span class="sub-chip">${fmtTime(track.duration || 0)}</span>
          <span class="sub-sep">•</span><span class="sub-chip" id="bpm-chip">${(chords.bpm || 0).toFixed(0)} BPM</span>
          <span class="sub-sep">•</span><span class="sub-chip">${chords.bars.length} mesures</span>
        </div>
      </div>
      <div class="px-actions">
        <div class="transpose-group" title="Transposition des accords (demi-tons)">
          <span class="tr-lbl">Transpo</span>
          <button class="t-btn-tr" id="btn-trans-down" title="Descendre d'un demi-ton">-1</button>
          <span class="tr-val" id="trans-val">0</span>
          <button class="t-btn-tr" id="btn-trans-up" title="Monter d'un demi-ton">+1</button>
          <button class="t-btn-enh" id="btn-trans-enh" title="Basculer dièses (#) / bémols (♭)">♭/#</button>
          <button class="t-btn-reset" id="btn-trans-reset" title="Rétablir tonalité d'origine">↺</button>
        </div>
      </div>
    </div>

    <div class="transport" id="transport">
      <!-- Ligne 1 : Lecture principale (toujours visible) -->
      <div class="transport-row row-playback">
        <button class="t-btn primary-cta" id="btn-play" title="Lecture / pause">▶</button>
        <button class="t-btn" id="btn-stop" title="Stop">■</button>
        <span class="t-time"><b id="t-cur">0:00</b> / <span id="t-tot">${fmtTime(track.duration)}</span></span>
        <input type="range" class="seekbar" id="seek" min="0" max="100" step="0.01" value="0" />
        <!-- Bouton flèche accordéon pour mobile -->
        <button class="t-btn t-btn-drawer" id="btn-transport-toggle" title="Afficher / masquer les réglages (vitesse, boucles, rythme)" aria-label="Réglages avancés">
          <span id="transport-arrow">▲</span>
        </button>
      </div>
      <!-- Tiroir accordéon : Vitesse, Boucles, Rythme -->
      <div class="transport-drawer" id="transport-drawer">
        <!-- Ligne 2 : Vitesse -->
        <div class="transport-row row-speed" title="Vitesse sans changement de hauteur">
          <span class="row-label">Vitesse</span>
          <div class="speed-group" id="speed-group">
            <button class="sp-btn" data-speed="0.5">0.5×</button>
            <button class="sp-btn" data-speed="0.75">0.75×</button>
            <button class="sp-btn" data-speed="0.9">0.9×</button>
            <button class="sp-btn active" data-speed="1">1.0×</button>
          </div>
        </div>
        <!-- Ligne 3 : Boucles A/B -->
        <div class="transport-row row-loop">
          <span class="row-label">Boucle</span>
          <div class="tr-group">
            <button class="t-btn" id="btn-loop" title="Activer / désactiver la boucle A/B">⤾</button>
            <button class="t-btn" id="btn-setA" title="Définir point A">A</button>
            <button class="t-btn" id="btn-setB" title="Définir point B">B</button>
            <span class="loop-info" id="loop-info">—</span>
          </div>
        </div>
        <!-- Ligne 4 : Métronome & recalibrage -->
        <div class="transport-row row-metro">
          <span class="row-label">Rythme</span>
          <div class="metro-group" title="Volume du métronome">
            <button class="t-btn" id="btn-metro" title="Activer / désactiver le métronome">♩</button>
            <input type="range" class="metro-vol" id="metro-vol" min="0" max="100" value="70" title="Volume du métronome" />
          </div>
          <button class="t-btn" id="btn-bar-shift" title="Décaler le 1er temps (anacrouse / aligner le temps 1)">⇄ T1</button>
        </div>
      </div>
    </div>

    <div class="px-grid">
      <!-- Accordéon 1 : Mixeur Stems -->
      <aside class="mixer accordion-panel" id="panel-mixer">
        <div class="mixer-title accordion-head" id="head-mixer" title="Cliquer pour replier / déplier le mixeur">
          <span class="accordion-chevron" id="chevron-mixer">▾</span>
          <span>Mixeur / stems</span>
        </div>
        <div class="accordion-body" id="body-mixer">
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
        </div>
      </aside>
      <!-- Accordéon 2 : Accords & Paroles -->
      <aside class="chord-now accordion-panel" id="vis-panel">
        <div class="accordion-head vis-head-bar" id="head-vis" title="Cliquer pour replier / déplier">
          <div class="vis-head-left">
            <span class="accordion-chevron" id="chevron-vis">▾</span>
            <span class="vis-head-label">Accords &amp; Paroles</span>
          </div>
          <div class="vis-tabs" id="vis-tabs">
            <button type="button" class="vis-tab-btn" id="vis-tab-chords" data-tab="chords">🎸 Accords</button>
            <button type="button" class="vis-tab-btn" id="vis-tab-lyrics" data-tab="lyrics">🎤 Paroles</button>
          </div>
        </div>
        <div class="accordion-body vis-content" id="body-vis">
          <!-- Vue Accords -->
          <div class="vis-pane" id="vis-pane-chords">
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
          </div>
          <!-- Vue Paroles (Karaoké 3 lignes) -->
          <div class="vis-pane hidden" id="vis-pane-lyrics">
            <div class="karaoke-stage" id="karaoke-stage">
              <div class="karaoke-stream" id="karaoke-stream">
                <div class="k-line k-prev" id="lyric-prev"></div>
                <div class="k-line k-cur" id="lyric-cur">En attente des paroles…</div>
                <div class="k-line k-next" id="lyric-next"></div>
              </div>
              <div class="noc hidden" id="karaoke-empty">Aucune parole disponible pour ce morceau</div>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <div class="grid-wrap" id="grid-wrap">
      <div class="grid-title" id="grid-header" title="Cliquer pour replier / déplier la grille d'accords">
        <div class="grid-title-left">
          <span class="grid-accordion-chevron" id="grid-chevron">▾</span>
          <span>Grille d'accords — Vue 16 mesures (anticipée)</span>
        </div>
        <div class="grid-cols-toggle" id="grid-tools">
          <button class="btn-reset-grid" id="btn-reset-grid" title="Annuler tous les sauts de ligne et revenir à la grille standard">↺ Réinitialiser</button>
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

  // Activation des 3 accordéons
  bindAccordion('#panel-mixer', '#head-mixer', '#chevron-mixer', 'guitarlab_mixer_collapsed');
  bindAccordion('#vis-panel', '#head-vis', '#chevron-vis', 'guitarlab_vis_collapsed');
  bindAccordion('#grid-wrap', '#grid-header', '#grid-chevron', 'guitarlab_grid_collapsed');

  const btnTransportToggle = $('#btn-transport-toggle');
  const transportDrawer = $('#transport-drawer');
  const transportArrow = $('#transport-arrow');
  btnTransportToggle?.addEventListener('click', () => {
    if (!transportDrawer) return;
    const isOpen = transportDrawer.classList.toggle('open');
    if (transportArrow) {
      transportArrow.textContent = isOpen ? '▼' : '▲';
    }
  });

  const engine = new AudioEngine(track);
  App.engine = engine;

  // 1. Initialiser les segments AVANT toute manipulation d'onglets
  App.lyricsSegments = Array.isArray(track.lyrics)
    ? track.lyrics
    : (track.lyrics?.segments || track.whisper?.segments || []);
  const hasLyrics = App.lyricsSegments.length > 0;
  $('#karaoke-stream')?.classList.toggle('hidden', !hasLyrics);
  $('#karaoke-empty')?.classList.toggle('hidden', hasLyrics);
  // 2. Gestion et bascule des onglets
  const savedTab = localStorage.getItem('guitarlab_vis_tab') || 'chords';
  function switchVisTab(tabName) {
    localStorage.setItem('guitarlab_vis_tab', tabName);
    const isChords = tabName === 'chords';
    $('#vis-tab-chords')?.classList.toggle('active', isChords);
    $('#vis-tab-lyrics')?.classList.toggle('active', !isChords);
    $('#vis-pane-chords')?.classList.toggle('hidden', !isChords);
    $('#vis-pane-lyrics')?.classList.toggle('hidden', isChords);
    if (!isChords) {
      lastLyricIndex = -2;
      lastIsInstrumental = null;
      updateLyricsUI(App.engine ? App.engine.currentTime : 0);
    }
  }
  $('#vis-tab-chords')?.addEventListener('click', () => switchVisTab('chords'));
  $('#vis-tab-lyrics')?.addEventListener('click', () => switchVisTab('lyrics'));
  switchVisTab(savedTab);
  // 3. Forcer un premier rendu immédiat des paroles si des segments existent
  if (hasLyrics) {
    updateLyricsUI(0);
  }

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
      return `<button class="section-pill ${style.cls}" data-m="${m}">${style.icon} ${esc(tag)} (m.${m})</button>`;
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
  $$('#speed-group .sp-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      engine.setSpeed(parseFloat(btn.dataset.speed));
      $$('#speed-group .sp-btn').forEach(b => b.classList.toggle('active', b === btn));
    });
  });
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
  // Charge les morceaux et les dossiers en parallèle, puis met en cache les
  // réponses dans `App` : le filtrage par onglet et le calcul des compteurs
  // rejouent sur les caches sans re-requêter le backend.
  let tracks = [];
  try { tracks = await API.tracks(); } catch (_) { /* backend indisponible */ }
  let folders = [];
  try { folders = await API.getFolders(); } catch (_) { /* backend indisponible */ }
  App.tracks = Array.isArray(tracks) ? tracks : [];
  App.folders = Array.isArray(folders) ? folders : [];
  renderFolderTabs();
  renderCards();
}

/* ------------------- onglets de dossiers + rendu des cartes ---------------- */
function folderTabHTML(folder, label, count) {
  const active = App.activeFolder === folder;
  // Seuls les dossiers personnalisés (pas « Tous » / « Non classés ») sont
  // supprimables d'un clic sur leur croix.
  const del = (folder !== FOLDER_ALL && folder !== FOLDER_UNCLASSIFIED)
    ? '<span class="folder-tab-del" title="Supprimer le dossier">×</span>' : '';
  return `<button type="button" class="folder-tab${active ? ' active' : ''}" data-folder="${esc(folder)}">
    <span class="folder-tab-label">${esc(label)}</span>
    <span class="folder-tab-count">${count}</span>${del}
  </button>`;
}

function renderFolderTabs() {
  const tabsBar = $('#library-folders');
  if (!tabsBar) return;
  const tracks = App.tracks || [];
  const counts = {};
  let unclassified = 0;
  tracks.forEach(t => {
    const f = (t.folder || '').trim();
    if (f) counts[f] = (counts[f] || 0) + 1;
    else unclassified += 1;
  });

  const parts = [folderTabHTML(FOLDER_ALL, 'Tous', tracks.length)];
  if (unclassified > 0) parts.push(folderTabHTML(FOLDER_UNCLASSIFIED, 'Non classés', unclassified));
  (App.folders || []).forEach(name => parts.push(folderTabHTML(name, name, counts[name] || 0)));
  parts.push('<button type="button" class="folder-add" id="btn-add-folder" title="Créer un nouveau dossier">＋ Dossier</button>');
  tabsBar.innerHTML = parts.join('');

  // Clic sur un onglet → change le filtre actif et re-rend les cartes.
  tabsBar.querySelectorAll('.folder-tab').forEach(tab => {
    tab.addEventListener('click', (ev) => {
      // La croix de suppression est gérée à part, on ne change pas d'onglet.
      if (ev.target.closest('.folder-tab-del')) return;
      App.activeFolder = tab.dataset.folder;
      renderFolderTabs();
      renderCards();
    });
  });
  // Suppression d'un dossier personnalisé.
  tabsBar.querySelectorAll('.folder-tab-del').forEach(del => {
    del.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const tab = del.closest('.folder-tab');
      const name = tab.dataset.folder;
      if (!name) return;
      if (confirm(`Supprimer le dossier « ${name} » ? Les morceaux passeront en « Non classés ».`)) {
        deleteFolderAndRefresh(name);
      }
    });
  });
  const addBtn = $('#btn-add-folder');
  if (addBtn) addBtn.addEventListener('click', createFolderFlow);
}

function renderCards() {
  const lib = $('#library');
  const empty = $('#empty-lib');
  const tracks = App.tracks || [];
  // Filtre les cartes selon l'onglet actif.
  const visible = filterTracksByFolder(tracks, App.activeFolder);
  const emptyTitle = empty.querySelector('p');
  const emptyHint = empty.querySelector('.dim');
  $('#lib-count').textContent = `${tracks.length} morceau${tracks.length > 1 ? 'x' : ''}`;
  if (tracks.length === 0) {
    // Bibliothèque vide : message générique.
    if (emptyTitle) emptyTitle.textContent = 'Aucun morceau pour le moment.';
    if (emptyHint) emptyHint.textContent = 'Lancez un premier décorticage ci-dessus pour remplir le labo.';
    empty.classList.remove('hidden');
  } else if (visible.length === 0) {
    // Dossier actif sans musique : ne pas afficher une grille vierge muette.
    const label = App.activeFolder === FOLDER_UNCLASSIFIED
      ? 'non classés' : `« ${App.activeFolder} »`;
    if (emptyTitle) emptyTitle.textContent = `Aucun morceau dans ${label}.`;
    if (emptyHint) emptyHint.textContent = 'Assignez des morceaux à ce dossier ou changez d’onglet.';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
  }
  lib.innerHTML = visible.map(renderCard).join('');
  lib.querySelectorAll('article.card').forEach(card => bindCardEvents(card));
}

function filterTracksByFolder(tracks, folder) {
  if (folder === FOLDER_ALL) return tracks;
  if (folder === FOLDER_UNCLASSIFIED) return tracks.filter(t => !(t.folder || '').trim());
  // Toutes les autres valeurs sont des noms de dossiers utilisateur (bruts) :
  // aucune égalité avec les clés système n'est possible (elles sont préfixées).
  return tracks.filter(t => (t.folder || '').trim() === folder);
}

function renderCard(t) {
  const title = cleanTitle(t.title);
  const ready = t.status === 'ready';
  // Pastille de statut : vert = prêt, rouge = erreur, orange = traitement.
  const infoCls = ready ? 'ready' : (t.status === 'error' ? 'error' : 'processing');
  const infoTxt = ready ? 'Prêt' : (t.status === 'error' ? 'Erreur' : (t.status || 'En cours'));
  const thumb = t.thumbnail
    ? `style="background-image:url('${esc(t.thumbnail)}')"`
    : '';
  const meta = [t.artist, t.duration ? fmtTime(t.duration) : null, t.bpm ? `${t.bpm.toFixed(0)} BPM` : null]
    .filter(Boolean).join(' · ');
  const folder = (t.folder || '').trim();
  // Carte épurée : plus de badge « PRÊT » ni de croix superposée ni de bouton
  // « Ouvrir le labo ». Toute la carte est cliquable ; les actions sont
  // regroupées dans le bouton contextuel « ••• ».
  return `
    <article class="card${ready ? '' : ' not-ready'}" data-id="${esc(t.id)}"
      data-title="${esc(title)}" data-artist="${esc(t.artist || '')}" data-folder="${esc(folder)}"
      tabindex="0" role="button" aria-label="Ouvrir ${esc(title)} dans le labo">
      <div class="thumb" ${thumb}>
        ${t.thumbnail ? '' : '<span class="fallback">🎸</span>'}
        <span class="card-play" aria-hidden="true"><span class="play-ico">▶</span></span>
      </div>
      <div class="card-body">
        <h3 title="${esc(title)}">${esc(title)}</h3>
        <div class="meta">
          <span class="status-dot ${infoCls}" title="Statut : ${esc(infoTxt)}" aria-label="Statut : ${esc(infoTxt)}"></span>
          <span class="meta-text">${esc(meta)}</span>
        </div>
        <div class="fade">${esc((t.stems || []).length)} stems — ${esc(t.source || '')}</div>
      </div>
      <button type="button" class="card-menu-btn" title="Actions" aria-label="Actions pour ${esc(title)}">•••</button>
    </article>`;
}

function bindCardEvents(card) {
  // Toute la carte est cliquable → on ouvre le lecteur. Le menu d'actions
  // « ••• » isole son clic pour ne pas déclencher la lecture.
  card.addEventListener('click', async (ev) => {
    if (ev.target.closest('.card-menu-btn') || ev.target.closest('.card-menu-dropdown')) return;
    try { await openPlayer(card.dataset.id); }
    catch (_) { alert('Impossible d’ouvrir ce morceau (traitement incomplet ?).'); }
  });
  // Accessibilité clavier : Entrée / Espace ouvrent aussi le lecteur.
  card.addEventListener('keydown', (ev) => {
    if (ev.target !== card) return;
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      card.click();
    }
  });
  const menuBtn = card.querySelector('.card-menu-btn');
  menuBtn?.addEventListener('click', (ev) => {
    ev.stopPropagation(); // isole le clic : ne pas déclencher le lecteur
    toggleCardMenu(menuBtn, card);
  });
}

/* ------------------------ menu contextuel « ••• » des cartes ----------------- */
// Référence du menu flottant actuellement ouvert (un seul à la fois).
let openCardMenu = null;

function closeCardMenu() {
  if (openCardMenu) { openCardMenu.remove(); openCardMenu = null; }
}

// Ouvre ou referme le menu flottant d'une carte. Le clic sur une entrée est
// isolé (stopPropagation) pour ne pas déclencher l'ouverture du lecteur.
function toggleCardMenu(btn, card) {
  closeCardMenu();
  const dropdown = document.createElement('div');
  dropdown.className = 'card-menu-dropdown';
  dropdown.setAttribute('role', 'menu');
  dropdown.innerHTML = `
    <button type="button" class="card-menu-item" data-action="folder" role="menuitem">Changer de dossier</button>
    <button type="button" class="card-menu-item" data-action="rename" role="menuitem">Renommer</button>
    <button type="button" class="card-menu-item danger" data-action="delete" role="menuitem">Supprimer</button>
  `;
  const r = btn.getBoundingClientRect();
  dropdown.style.left = Math.max(8, Math.min(r.left - 8, window.innerWidth - 220)) + 'px';
  dropdown.style.top = (r.bottom + 6) + 'px';
  dropdown.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const item = ev.target.closest('.card-menu-item');
    if (!item) return;
    const action = item.dataset.action;
    const id = card.dataset.id;
    const title = card.dataset.title || 'ce morceau';
    const artist = card.dataset.artist || '';
    const folder = card.dataset.folder || '';
    closeCardMenu();
    if (action === 'delete') {
      if (!confirm(`Supprimer « ${title} » de la bibliothèque ?`)) return;
      API.remove(id).then(() => refreshLibrary())
        .catch(() => alert('Suppression impossible (backend injoignable ?).'));
    } else if (action === 'rename') {
      openRenameDialog(id, title, artist);
    } else if (action === 'folder') {
      openFolderAssignDialog(id, folder);
    }
  });
  document.body.appendChild(dropdown);
  openCardMenu = dropdown;
}

// Dialogue d'assignation d'un dossier via le menu « ••• ».
function openFolderAssignDialog(id, currentFolder) {
  const veil = document.createElement('div');
  veil.className = 'modal-veil';
  const folders = App.folders || [];
  veil.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <h3>Changer de dossier</h3>
        <button type="button" class="btn ghost" data-close>✕</button>
      </div>
      <div class="modal-body">
        <label class="field-label" for="folder-assign">Dossier</label>
        <select id="folder-assign" class="text-input">
          <option value="">Non classé</option>
          ${folders.map(f => `<option value="${esc(f)}"${f === currentFolder ? ' selected' : ''}>${esc(f)}</option>`).join('')}
        </select>
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-close>Annuler</button>
          <button type="button" class="btn primary" id="folder-assign-save">Enregistrer</button>
        </div>
      </div>
    </div>`;
  veil.addEventListener('click', (e) => {
    if (e.target === veil || e.target.closest('[data-close]')) veil.remove();
  });
  const save = veil.querySelector('#folder-assign-save');
  save?.addEventListener('click', async () => {
    const val = veil.querySelector('#folder-assign').value;
    try {
      await API.updateTrackMeta(id, { folder: val });
      veil.remove();
      refreshLibrary();
    } catch (_) {
      alert('Assignation au dossier impossible.');
    }
  });
  document.body.appendChild(veil);
}

/* ------------------ dialogue « renommer » + gestion des dossiers ------------ */
function openRenameDialog(id, title, artist) {
  const veil = document.createElement('div');
  veil.className = 'modal-veil';
  veil.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <h3>Renommer le morceau</h3>
        <button type="button" class="btn ghost" data-close>✕</button>
      </div>
      <div class="modal-body">
        <label class="field-label" for="rename-title">Titre</label>
        <input id="rename-title" class="text-input" type="text" value="${esc(title)}" maxlength="200" />
        <label class="field-label" for="rename-artist">Artiste</label>
        <input id="rename-artist" class="text-input" type="text" value="${esc(artist)}" maxlength="200" />
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-close>Annuler</button>
          <button type="button" class="btn primary" id="rename-save">Enregistrer</button>
        </div>
      </div>
    </div>`;
  veil.addEventListener('click', (e) => {
    if (e.target === veil || e.target.closest('[data-close]')) veil.remove();
  });
  const save = veil.querySelector('#rename-save');
  save.addEventListener('click', async () => {
    const titleVal = veil.querySelector('#rename-title').value.trim();
    const artistVal = veil.querySelector('#rename-artist').value.trim();
    // Même garde que le serveur : un titre vide est refusé.
    if (!titleVal) { alert('Le titre ne peut pas être vide.'); return; }
    try {
      await API.updateTrackMeta(id, { title: titleVal, artist: artistVal });
      veil.remove();
      refreshLibrary();
    } catch (_) {
      alert('Renommage impossible (backend injoignable ?).');
    }
  });
  document.body.appendChild(veil);
  const input = veil.querySelector('#rename-title');
  if (input) { input.focus(); input.select(); }
}

async function createFolderFlow() {
  const name = prompt('Nom du nouveau dossier :');
  if (!name) return;
  try {
    await API.createFolder(name.trim());
    refreshLibrary();
  } catch (_) {
    alert('Création du dossier impossible (backend injoignable ?).');
  }
}

async function deleteFolderAndRefresh(name) {
  try {
    await API.deleteFolder(name);
    // Si l'onglet supprimé était actif, on retombe sur « Tous ».
    if (App.activeFolder === name) App.activeFolder = FOLDER_ALL;
    refreshLibrary();
  } catch (_) {
    alert('Suppression du dossier impossible.');
  }
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
  // Mémorise le job en cours : permet de reprendre son suivi après un
  // rechargement de page ou une reconnexion (voir init()).
  localStorage.setItem('guitarlab_active_job', id);
  jobRender({ id, status: 'queued', progress: 0, logs: ['En attente du backend…'] }, true);

  let usingWs = false;
  let finished = false;
  const finish = (st) => {
    if (finished) return;
    finished = true;
    // Le traitement est terminé : plus besoin de le reprendre à la prochaine
    // ouverture, on nettoie l'identifiant mémorisé.
    localStorage.removeItem('guitarlab_active_job');
    setTimeout(() => {
      refreshLibrary();
      if (autoOpen && st === 'ready') openPlayer(id).catch(() => {});
      if (st === 'error') $('#form-hint').textContent = 'Le traitement a échoué — voir le journal ci-dessus.';
    }, 700);
  };

  const iv = setInterval(async () => {
    if (usingWs || finished) return;
    try {
      const s = await API.status(id);
      if (s && s.status !== 'unknown') jobRender(s);
      if (s && (s.status === 'ready' || s.status === 'error')) finish(s.status);
    } catch (e) {
      // 404 : le morceau n'existe plus (supprimé entre-temps) → on stoppe le
      // suivi et on nettoie `guitarlab_active_job` via `finish('error')`.
      if (e && e.status === 404) finish('error');
      // sinon (backend pas encore joignable) : on réessaiera au tick suivant.
    }
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

  // Mesure la hauteur de la barre du haut pour caler le lecteur sticky en dessous
  const topbarEl = document.querySelector('.topbar');
  function syncTopbarH() {
    if (topbarEl) document.documentElement.style.setProperty('--topbar-h', topbarEl.offsetHeight + 'px');
  }
  syncTopbarH();
  window.addEventListener('resize', syncTopbarH);

  // Fermeture du menu contextuel « ••• » au clic en dehors (un seul gestionnaire,
  // délégué au document, pour éviter les fuites de listeners par ouverture).
  document.addEventListener('click', (ev) => {
    if (openCardMenu && !openCardMenu.contains(ev.target) && !ev.target.closest('.card-menu-btn')) {
      closeCardMenu();
    }
  });

  // chip device & version dynamique
  try {
    const h = await API.health();
    const chip = $('#device-chip');
    if (chip) {
      chip.textContent = h.device.toUpperCase();
      if (h.device !== 'cuda') chip.classList.add('cpu');
    }
    if (h.version) {
      const vEl = $('#app-version');
      if (vEl) vEl.textContent = h.version;
    }
  } catch (_) {
    const chip = $('#device-chip');
    if (chip) chip.textContent = 'OFFLINE';
  }

  // navigation bibliothèque
  $('#btn-nav-home')?.addEventListener('click', showHome);
  $('#btn-brand')?.addEventListener('click', showHome);

  /* Gestion du thème Studio / Xbox */
  const themeBtn = $('#btn-theme-toggle');
  const themeLabel = $('#theme-label');
  function updateThemeUI() {
    const isXbox = document.documentElement.getAttribute('data-theme') === 'xbox';
    if (themeLabel) themeLabel.textContent = isXbox ? 'Studio' : 'Xbox';
    if (themeBtn) themeBtn.title = isXbox ? 'Passer au thème Studio' : 'Passer au thème Xbox';
  }
  if (themeBtn) {
    updateThemeUI();
    themeBtn.addEventListener('click', () => {
      const isXbox = document.documentElement.getAttribute('data-theme') === 'xbox';
      if (isXbox) {
        document.documentElement.removeAttribute('data-theme');
        localStorage.removeItem('guitarlab_theme');
      } else {
        document.documentElement.setAttribute('data-theme', 'xbox');
        localStorage.setItem('guitarlab_theme', 'xbox');
      }
      updateThemeUI();
    });
  }

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

  // Reprise automatique d'un décorticage en cours : après un rechargement de
  // page ou une reconnexion, on ré-ouvre le terminal et on reprend le suivi
  // (via l'API à défaut, sinon via l'identifiant mémorisé dans localStorage).
  try {
    const actives = await API.activeJobs();
    const resumeId = (actives && actives.length) ? actives[0].id
      : localStorage.getItem('guitarlab_active_job');
    if (resumeId) {
      localStorage.setItem('guitarlab_active_job', resumeId);
      monitorJob(resumeId, false); // autoOpen=false : pas d'ouverture du lecteur
    } else {
      localStorage.removeItem('guitarlab_active_job');
    }
  } catch (_) {
    // Backend injoignable : on se replie sur l'identifiant mémorisé.
    const resumeId = localStorage.getItem('guitarlab_active_job');
    if (resumeId) monitorJob(resumeId, false);
  }

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