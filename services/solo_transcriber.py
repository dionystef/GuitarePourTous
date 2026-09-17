"""Transcription du lead/solo de guitare via Spotify basic-pitch.

Sortie dans ``<track_dir>/`` :
  - ``guitar_solo.mid``  — MIDI polyphonique (notes, vélocités, timing)
  - ``guitar_tab.txt``   — tablature ASCII 6 cordes (accordage standard)
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np

logger = logging.getLogger("guitarlab.solo")

OPEN_MIDI = [40, 45, 50, 55, 59, 64]   # E2 A2 D3 G3 B3 E4 (corde 6 → 1)
STRINGS = ["e", "B", "G", "D", "A", "E"]  # affichage haut → bas (corde 1 → 6)
MAX_FRET = 15
LOW_PITCH = 40    # E2
HIGH_PITCH = 88   # E6 (au-delà, hors manche raisonnable)


def _pitch_of(freq: float) -> int:
    """Fr. Hz → MIDI (conservé pour l'ancien format de note_events)."""
    if not freq or freq <= 0:
        return 0
    return int(round(69.0 + 12.0 * math.log2(freq / 440.0)))


def _note_events_to_tuples(note_events):
    """Normalise la sortie note_events de basic-pitch en [(start, end, pitch_midi)]."""
    notes = []
    if not note_events:
        return notes
    for item in note_events:
        try:
            s = float(item[0])
            e = float(item[1])
            p = int(round(float(item[2])))
            if p > 0 and math.isfinite(s) and math.isfinite(e):
                notes.append((s, max(s, e), p))
        except (TypeError, ValueError, IndexError):
            continue
    return notes


def _fret_for(pitch: int, anchor: int):
    """Choisit (corde, case) en minimisant l'écart avec la position courante."""
    best = None
    for s, open_pc in enumerate(OPEN_MIDI):
        f = pitch - open_pc
        if 0 <= f <= MAX_FRET and LOW_PITCH <= pitch <= HIGH_PITCH:
            penalty = abs(f - anchor) + (4 if f >= 13 else 0)
            if best is None or penalty < best[0]:
                best = (penalty, s, f)
    if best is None:
        return None, -1          # note hors manche → muette
    return best[1], best[2]


def _estimate_bpm(starts: np.ndarray) -> float:
    if starts.size < 2:
        return 120.0
    gaps = np.diff(starts[np.argsort(starts)])
    gaps = gaps[(gaps > 0.04) & (gaps < 2.5)]
    med = float(np.median(gaps)) if gaps.size else 0.5
    bpm = 60.0 / med if med > 0 else 120.0
    while bpm < 50:
        bpm *= 2.0
    while bpm > 240:
        bpm /= 2.0
    return bpm


def build_tab(note_events, bpm: float | None = None) -> str:
    """Génère une tablature ASCII à partir des notes ``(start, end, pitch)``."""
    notes = [(s, max(float(s), float(e)), int(p))
             for s, e, p in _note_events_to_tuples(note_events)
             if p is not None and int(round(float(p))) > 0]
    if not notes:
        return "# Aucune note de solo détectée par basic-pitch.\n"

    notes.sort(key=lambda n: (n[0], n[1]))
    if not bpm:
        bpm = _estimate_bpm(np.array([n[0] for n in notes]))

    grid = 15.0 / bpm                       # double-croche (16e) en secondes
    total = max(e for _, e, _ in notes)
    ncols = int(math.ceil(total / grid)) + 1
    cells = [["-"] * 6 for _ in range(ncols)]

    anchor = 4
    for (s, _e, p) in notes:                 # _e : tenue, gérée par le « - »
        col = int(round(s / grid))
        if not (0 <= col < ncols):
            continue
        si, fret = _fret_for(p, anchor)
        if fret is not None and fret >= 0 and col < ncols:
            if cells[col][si] == "-":
                cells[col][si] = str(fret)
            anchor = fret

    def line(str_idx: int) -> str:
        out = []
        for c, cell in enumerate(cells):
            if c and c % 4 == 0:
                out.append("|")
            v = cell[str_idx]
            out.append(v.rjust(2) if v != "-" else "  ")
        return "".join(out)

    header = [
        f"# Tablature générée (basic-pitch) — {len(notes)} notes, {bpm:.0f} BPM",
        f"# Accordage standard EADGBe — quadrillage : {grid * 1000:.0f} ms (16e)",
    ]
    body = [f"{s}|{line(i)}" for i, s in enumerate(STRINGS)]
    return "\n".join(header + ["", *body, ""])


def transcribe_solo(wav_path: Path, track_dir: Path, bpm: float | None = None):
    """Extrait le lead de guitare → MIDI + tablature.

    Retourne ``(midi_path, tab_path, nb_notes)``.
    """
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import predict

    logger.info("basic-pitch : prédiction sur %s", wav_path.name)
    _model_output, midi_data, note_events = predict(
        str(wav_path),
        ICASSP_2022_MODEL_PATH,
        onset_threshold=0.42,
        frame_threshold=0.20,
        minimum_note_length=90,            # ms
        minimum_frequency=82.41,           # E2
        maximum_frequency=1046.50,         # C6
    )

    notes = _note_events_to_tuples(note_events)

    midi_path = track_dir / "guitar_solo.mid"
    midi_data.write(str(midi_path))

    tab_path = track_dir / "guitar_tab.txt"
    tab_path.write_text(build_tab(notes, bpm), encoding="utf-8")

    logger.info("Solo extrait : %d notes → %s", len(notes), midi_path.name)
    return midi_path, tab_path, int(len(notes))