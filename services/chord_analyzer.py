"""Analyse harmonique : battements (tempo) + grille d'accords synchronisée.

Stratégie, du meilleur au plus robuste :
1. madmom — ``RNNChordFeatureProcessor`` + ``CRFChordRecognitionProcessor``
   (réseaux pré-entraînés, détection beats/accords jointe).
2. librosa — beat tracking + chroma CENS + corrélation avec des gabarits
   d'accords (triades/septièmes normalisés), quantifiés par mesure 4/4.

Sortie JSON (mêmes clés quoi qu'il arrive) :
    {bpm, time_signature, beats_per_bar, duration, bars: [{measure, start,
     end, chord, times, sig}]}
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# Compat comportement Python 3.10 pour madmom (CNN/CRF/RNN pré-entraînés) :
#   - collections.Sequence/… ont été déplacés vers collections.abc en 3.10 ;
#   - np.float / np.int ont été supprimés dans les numpy récents.
# À appliquer AVANT tout import de madmom.
# --------------------------------------------------------------------------- #
import collections
import collections.abc
for _n in ['MutableSequence', 'Sequence', 'Mapping', 'MutableMapping',
           'Iterable', 'Callable', 'Container']:
    setattr(collections, _n, getattr(collections.abc, _n, None))

import numpy as np
if not hasattr(np, 'float'):
    np.float = float
if not hasattr(np, 'int'):
    np.int = int

import logging
from collections import Counter
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger("guitarlab.chords")

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
BEATS_PER_BAR = 4  # 4/4 par défaut


# --------------------------------------------------------------------------- #
# Normalisation des étiquettes d'accords
# --------------------------------------------------------------------------- #
_FLAT = {"db": "c#", "eb": "d#", "gb": "f#", "ab": "g#", "bb": "a#",
         "cb": "b", "fb": "e"}


def _norm_root(r: str) -> str:
    low = r.strip().lower()
    return _FLAT.get(low, (low[0].upper() + low[1:]) if low else "C")


def _norm_quality(q: str) -> str:
    table = {
        "": "", "maj": "", "major": "", "m": "m", "min": "m", "minor": "m",
        "7": "7", "maj7": "maj7", "m7": "m7", "dim": "dim", "aug": "aug",
        "sus": "sus4", "sus4": "sus4", "sus2": "sus2", "6": "6", "m6": "m6",
        "9": "9", "m7b5": "m7b5", "mmaj7": "mmaj7", "maj9": "maj9",
    }
    return table.get(q.strip().lower(), "")


def _norm_chord(raw: str) -> str:
    raw = (raw or "N").strip()
    if not raw or raw.upper() in ("N", "NC", "N.C.", "NONE", "NO CHORD", "NOCHORD"):
        return "N"
    if ":" in raw:
        root_raw, qual_raw = raw.split(":", 1)
        return _norm_root(root_raw) + _norm_quality(qual_raw)
    return _norm_root(raw[0]) + _norm_quality(raw[1:])


# --------------------------------------------------------------------------- #
# Gabarits PCP pour le repli librosa
# --------------------------------------------------------------------------- #
def _templates() -> List[Tuple[str, np.ndarray]]:
    def pcp(intervals: List[int]) -> np.ndarray:
        v = np.zeros(12, dtype=float)
        for i in intervals:
            v[i % 12] += 1.0
        return v / max(float(np.linalg.norm(v)), 1e-9)

    return [
        ("", pcp([0, 4, 7])),
        ("m", pcp([0, 3, 7])),
        ("7", pcp([0, 4, 7, 10])),
        ("maj7", pcp([0, 4, 7, 11])),
        ("m7", pcp([0, 3, 7, 10])),
        ("dim", pcp([0, 3, 6])),
        ("sus4", pcp([0, 5, 7])),
    ]


def _match_chroma(chroma: np.ndarray, templates) -> str:
    c = chroma - chroma.mean()
    best_score, best = -1.0, "N"
    for shift in range(12):
        for name, tpl in templates:
            score = float(c @ np.roll(tpl, shift))
            if score > best_score:
                best_score, best = score, NOTE_NAMES[shift] + name
    return best if best_score > 0.015 else "N"


# --------------------------------------------------------------------------- #
# Détection beats + accords
# --------------------------------------------------------------------------- #
def _madmom_analysis(audio_path: Path):
    """Renvoie ``(beats, labels)`` via madmom (RNN+CRF / CNN+CRF), sinon ``None``."""
    try:
        from madmom.features.beats import (DBNBeatTrackingProcessor,
                                           RNNBeatProcessor)
        from madmom.features.chords import (CNNChordFeatureProcessor,
                                            CRFChordRecognitionProcessor)
    except Exception as exc:  # ImportError, build KO, modèle absent…
        logger.info("madmom indisponible (%s) → repli librosa", exc)
        return None
    try:
        beat_proc = DBNBeatTrackingProcessor(fps=100)
        beats = beat_proc(RNNBeatProcessor()(str(audio_path)))
        feat = CNNChordFeatureProcessor()(str(audio_path))
        decoded = CRFChordRecognitionProcessor()(feat)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analyse madmom en échec (%s) → repli librosa", exc)
        return None

    chord_segments = []
    for item in decoded:
        seq = tuple(item) if isinstance(item, (tuple, list, np.void)) else None
        if not seq or len(seq) < 3:
            continue
        try:
            t_s = float(seq[0])
            t_e = float(seq[1])
            lbl = _norm_chord(str(seq[2]))
            chord_segments.append((t_s, t_e, lbl))
        except (ValueError, TypeError):
            continue
    beats = np.asarray([b for b in beats if np.isfinite(b)], dtype=float)
    if len(beats) < 2 or not chord_segments:
        return None
    # Mappe chaque battement sur son intervalle temporel réel
    labels = []
    seg_idx = 0
    num_segs = len(chord_segments)
    for b in beats:
        while seg_idx < num_segs - 1 and b >= chord_segments[seg_idx][1]:
            seg_idx += 1
        s, e, lbl = chord_segments[seg_idx]
        labels.append(lbl if (s <= b <= e) else "N")
    return beats, labels


def _librosa_analysis(audio_path: str, sr: int = 22050):
    """Repli : beat tracking + chroma CENS + gabarits PCP."""
    import librosa

    y, sr = librosa.load(audio_path, sr=sr, mono=True)
    if y.size == 0:
        raise ValueError("fichier audio vide ou illisible")
    if y.shape[0] < sr:  # moins d'une seconde
        raise ValueError("fichier trop court pour l'analyse harmonique")

    tempo, frames = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    tempo = float(np.atleast_1d(tempo)[0])
    beats = librosa.frames_to_time(frames, sr=sr)

    if beats.size < 2:
        beat_len = 60.0 / (tempo if tempo > 0 else 120.0)
        beats = np.arange(0.0, y.shape[0] / sr, beat_len)

    chroma = librosa.feature.chroma_cens(y=y, sr=sr) + 1e-6
    chroma = chroma / chroma.sum(axis=0, keepdims=True)

    ends = np.concatenate([beats[1:], [beats[-1] + float(np.median(np.diff(beats)))]])
    bf = librosa.time_to_frames(beats, sr=sr).astype(int)
    ef = np.clip(librosa.time_to_frames(ends, sr=sr).astype(int) + 1,
                 bf + 1, chroma.shape[1])

    templates = _templates()
    labels = [_match_chroma(chroma[:, bf[i]:ef[i]].mean(axis=1), templates)
              for i in range(len(beats))]
    return beats, labels


# --------------------------------------------------------------------------- #
# Statistiques & construction JSON
# --------------------------------------------------------------------------- #
def _bpm_from_beats(beats: np.ndarray) -> float:
    d = np.diff(np.asarray(beats, dtype=float))
    d = d[d > 0.05]
    med = float(np.median(d)) if d.size else 0.5
    bpm = 60.0 / med if med > 0 else 120.0
    while bpm < 50:
        bpm *= 2.0
    while bpm > 240:
        bpm /= 2.0
    return bpm


def _build_json(beats, labels, bpm: float, duration: float) -> dict:
    beats = np.asarray(beats, dtype=float)
    order = np.argsort(beats)
    beats = beats[order]
    labels = [labels[int(i)] for i in order]

    if bpm <= 0:
        bpm = _bpm_from_beats(beats)
    med_beat = 60.0 / bpm

    bars = []
    for start in range(0, len(beats), BEATS_PER_BAR):
        seg_b = beats[start:start + BEATS_PER_BAR]
        seg_l = labels[start:start + BEATS_PER_BAR]
        if not len(seg_b):
            continue
        end = (float(seg_b[-1] + (seg_b[-1] - seg_b[-2])) if len(seg_b) > 1
               else float(seg_b[0] + med_beat))
        end = min(max(end, float(seg_b[-1]) + 0.1), float(duration))
        real = [l for l in seg_l if l != "N"]
        chord = Counter(real).most_common(1)[0][0] if real else "N"
        bars.append({
            "measure": len(bars) + 1,
            "start": round(float(seg_b[0]), 3),
            "end": round(end, 3),
            "chord": chord,
            "times": [round(float(t), 3) for t in seg_b],
            "sig": list(seg_l),
        })

    # Mesures adjacentes : jamais de chevauchement (fin = début de la suivante).
    for i in range(len(bars) - 1):
        bars[i]["end"] = round(float(bars[i + 1]["start"]), 3)

    return {
        "bpm": round(bpm, 1),
        "time_signature": "4/4",
        "beats_per_bar": BEATS_PER_BAR,
        "duration": round(float(duration), 3),
        "bars": bars,
    }


def analyze(audio_path: Path) -> dict:
    """Point d'entrée : extrait la grille d'accords synchronisée d'un WAV."""
    import soundfile as sf

    audio_path = Path(audio_path)
    duration = float(sf.info(str(audio_path)).duration)

    res = _madmom_analysis(audio_path)
    if res is None:
        beats, labels = _librosa_analysis(str(audio_path))
    else:
        beats, labels = res

    bpm = _bpm_from_beats(beats)
    logger.info("Analyse harmonique : %s BPM, %d battements, %d mesures",
                round(bpm, 1), len(beats), int(np.ceil(len(beats) / BEATS_PER_BAR)))
    return _build_json(beats, labels, bpm, duration)