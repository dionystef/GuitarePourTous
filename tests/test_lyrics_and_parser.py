"""Tests du fournisseur de paroles : parseur LRC, nettoyage titre, timeout et orchestration."""
import time
from pathlib import Path

import pytest

import services.lyrics_provider as lp
from services.lyrics_provider import parse_lrc, clean_title, _run_with_timeout, resolve_lyrics


# --------------------------------------------------------------------------- #
# parse_lrc
# --------------------------------------------------------------------------- #
def test_parse_lrc_basic():
    lrc = """[ti:titre]\r\n[ar:artiste]\r\n[00:01.50]Hello\r\n[00:04.60]world\r\n"""
    out = parse_lrc(lrc)
    assert out == [
        {"start": 1.5, "end": 4.6, "text": "Hello"},
        {"start": 4.6, "end": 8.6, "text": "world"},
    ]


def test_parse_lrc_skips_metadata_and_blank():
    out = parse_lrc("[ar:x]\n\n[00:00.00]A\n\n[00:05.00]B\n")
    assert [e["text"] for e in out] == ["A", "B"]


def test_parse_lrc_long_gap_clamped():
    # Écart > 8 s → la fin est ramenée à start + 4s.
    out = parse_lrc("[00:00.00]A\n[00:30.00]B\n")
    assert out[0]["end"] == 4.0


# --------------------------------------------------------------------------- #
# clean_title
# --------------------------------------------------------------------------- #
def test_clean_title_strips_bracketed_keywords():
    assert clean_title("Song (Official Video)") == "Song"
    assert clean_title("Song - Remastered [Clip]") == "Song Remastered"


# --------------------------------------------------------------------------- #
# _run_with_timeout
# --------------------------------------------------------------------------- #
def test_run_with_timeout_returns_result():
    assert _run_with_timeout(lambda: 42, 2) == 42


def test_run_with_timeout_raises_inner_exception():
    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _run_with_timeout(boom, 1)


def test_run_with_timeout_raises_on_timeout():
    def slow():
        time.sleep(0.3)
        return 1

    with pytest.raises(TimeoutError):
        _run_with_timeout(slow, 0.05)


# --------------------------------------------------------------------------- #
# resolve_lyrics (orchestrateur mocké)
# --------------------------------------------------------------------------- #
def test_resolve_lyrics_lrclib(monkeypatch, tmp_path):
    monkeypatch.setattr(lp, "fetch_lrclib", lambda *a, **k: [{"start": 0.0, "text": "x"}])
    out = tmp_path / "lyrics.json"
    lyrics, src = resolve_lyrics("Titre", "Artiste", 100, None, out)
    assert src == "lrclib"
    assert lyrics == [{"start": 0.0, "text": "x"}]
    assert out.exists()


def test_resolve_lyrics_whisper_fallback(monkeypatch, tmp_path):
    # Pas de paroles LRCLIB mais stem vocal présent → fallback Whisper.
    monkeypatch.setattr(lp, "fetch_lrclib", lambda *a, **k: None)
    monkeypatch.setattr(lp, "transcribe_vocals_fallback",
                        lambda *a, **k: [{"start": 0.0, "text": "parole"}])
    vocals = tmp_path / "vocals.mp3"
    vocals.write_bytes(b"x")
    out = tmp_path / "lyrics.json"
    lyrics, src = resolve_lyrics("Titre", "", 0, vocals, out)
    assert src == "whisper"
    assert lyrics == [{"start": 0.0, "text": "parole"}]


def test_resolve_lyrics_none(monkeypatch, tmp_path):
    monkeypatch.setattr(lp, "fetch_lrclib", lambda *a, **k: None)
    out = tmp_path / "lyrics.json"
    lyrics, src = resolve_lyrics("Titre", "", 0, None, out)
    assert src == "none"
    assert lyrics == []
