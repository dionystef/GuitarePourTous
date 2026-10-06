"""Tests de la validation des fichiers audio uploadés (extension + magic bytes)."""
from pathlib import Path

import services.downloader as dl

# Signatures binaires minimales reconnues (header de 12 octets au plus).
AUDIO_SIGNATURES = {
    "mp3_id3": (b"ID3\x03\x00\x00\x00\x00\x00\x00", ".mp3"),
    "mp3_frame": (b"\xff\xfb\x90\x00\x00\x00\x00\x00", ".mp3"),
    "wav": (b"RIFF\x24\x00\x00\x00WAVEfmt ", ".wav"),
    "flac": (b"\x66\x4c\x61\x43\x00\x00\x00\x22", ".flac"),
    "ogg": (b"OggS\x00\x02\x00\x00\x00\x00", ".ogg"),
    "m4a_ftyp": (b"\x00\x00\x00\x18ftypM4A \x00\x00", ".m4a"),
    "webm_ebml": (b"\x1a\x45\xdf\xa3\x93\x42\x86\x81", ".webm"),
    "aac_adts": (b"\xff\xf1\x50\x80\x00\x00\x00\x00", ".aac"),
}


# --------------------------------------------------------------------------- #
# is_supported_audio_by_ext
# --------------------------------------------------------------------------- #
def test_ext_allowlist():
    assert dl.is_supported_audio_by_ext("song.mp3") is True
    assert dl.is_supported_audio_by_ext("song.wav") is True
    assert dl.is_supported_audio_by_ext("song.FLAC") is True   # insensible à la casse
    assert dl.is_supported_audio_by_ext("song.m4a") is True
    assert dl.is_supported_audio_by_ext("song.webm") is True


def test_ext_rejected():
    assert dl.is_supported_audio_by_ext("song.exe") is False
    assert dl.is_supported_audio_by_ext("song.mp3.exe") is False
    assert dl.is_supported_audio_by_ext("noext") is False
    assert dl.is_supported_audio_by_ext("") is False


# --------------------------------------------------------------------------- #
# _has_audio_magic
# --------------------------------------------------------------------------- #
def test_has_audio_magic_recognized(tmp_path):
    for name, (sig, ext) in AUDIO_SIGNATURES.items():
        p = tmp_path / f"{name}{ext}"
        p.write_bytes(sig)
        assert dl._has_audio_magic(p) is True, name


def test_has_audio_magic_rejects_non_audio(tmp_path):
    p = tmp_path / "fake.mp3"
    p.write_bytes(b"MZ\x90\x00 executable content")
    assert dl._has_audio_magic(p) is False


def test_has_audio_magic_empty_file(tmp_path):
    p = tmp_path / "empty.mp3"
    p.write_bytes(b"")
    assert dl._has_audio_magic(p) is False


# --------------------------------------------------------------------------- #
# is_supported_audio (extension ET magic bytes)
# --------------------------------------------------------------------------- #
def test_is_supported_audio_ok(tmp_path):
    p = tmp_path / "ok.mp3"
    p.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    assert dl.is_supported_audio(p) is True


def test_is_supported_audio_rejects_bad_magic_with_good_ext(tmp_path):
    p = tmp_path / "fake.mp3"
    p.write_bytes(b"<html>not audio</html>")
    assert dl.is_supported_audio(p) is False


def test_is_supported_audio_rejects_good_magic_with_bad_ext(tmp_path):
    # Signature audio valide mais extension hors allowlist → refus.
    p = tmp_path / "x.exe"
    p.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    assert dl.is_supported_audio(p) is False
