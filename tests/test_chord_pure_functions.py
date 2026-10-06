"""Tests des fonctions pures de l'analyse harmonique (chord_analyzer)."""
import numpy as np

from services.chord_analyzer import _norm_chord, _bpm_from_beats, _match_chroma, _templates


def test_norm_chord_basic():
    assert _norm_chord("C") == "C"
    assert _norm_chord("Am") == "Am"
    assert _norm_chord("G7") == "G7"
    assert _norm_chord("C:maj7") == "Cmaj7"


def test_norm_chord_minor_and_flat_quality():
    assert _norm_chord("Bb:m") == "a#m"
    assert _norm_chord("c:m") == "Cm"


def test_norm_chord_no_chord():
    assert _norm_chord("") == "N"
    assert _norm_chord("N") == "N"
    assert _norm_chord("N.C.") == "N"
    assert _norm_chord("NO CHORD") == "N"


def test_bpm_from_beats():
    assert round(_bpm_from_beats(np.array([0, 1, 2, 3, 4, 5, 6]))) == 60
    assert round(_bpm_from_beats(np.array([0, 0.3, 0.6, 0.9]))) == 200
    bpm = _bpm_from_beats(np.array([0, 2, 4, 6]))  # 30 BPM brut → clampé
    assert 50 <= bpm <= 240


def test_match_chroma_detects_major():
    templates = _templates()
    c = np.zeros(12)
    c[0] = 1.0   # C
    c[4] = 1.0   # E
    c[7] = 1.0   # G
    assert _match_chroma(c, templates) == "C"


def test_match_chroma_detects_minor():
    templates = _templates()
    c = np.zeros(12)
    c[0] = 1.0   # C
    c[3] = 1.0   # Eb
    c[7] = 1.0   # G
    assert _match_chroma(c, templates) == "Cm"
