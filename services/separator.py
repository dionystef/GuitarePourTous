"""Séparation de sources (Demucs) : stems WAV → MP3 pour le streaming web.

Structure produite dans ``<track_dir>/stems/`` :
  - ``mix.mp3``            — piste originale complète
  - ``vocals.mp3``         — voix
  - ``drums.mp3``          — batterie
  - ``bass.mp3``           — basse
  - ``guitar.mp3``         — guitare
  - ``other.mp3``          — le reste
  - ``piano.mp3``          — piano (modèle 6 sources uniquement)
  - ``raw/``               — stems WAV tuiles ; le stem ``guitar.wav`` est
                             conservé pour la transcription du solo.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("guitarlab.separator")

MODEL_6S = "htdemucs_6s"
MODEL_4S = "htdemucs"

SHIFTS = int(os.environ.get("DEMUCS_SHIFTS", "1"))
SEGMENT = os.environ.get("DEMUCS_SEGMENT", "7")
OVERLAP = os.environ.get("DEMUCS_OVERLAP", "0.25")
MP3_BITRATE = os.environ.get("STEM_MP3_BITRATE", "128k")

# Ordre d'apparition dans l'interface (avantages = canaux du mixeur).
STEM_ORDER = ["mix", "guitar", "bass", "drums", "vocals", "piano", "other"]
KEEP_WAV = {"guitar"}  # stems tuiles conservés (transcription du solo)


def _run(cmd: list[str], label: str) -> None:
    logger.info("%s → %s", label, " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-900:]
        raise RuntimeError(f"{label} en échec (code {proc.returncode}) : {tail}")
    return None


def _encode_mp3(wav: Path, mp3: Path) -> None:
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(wav), "-c:a", "libmp3lame", "-b:a", MP3_BITRATE,
        "-ac", "2", str(mp3),
    ], "MP3")


def _collect_stems(out_dir: Path) -> dict[str, Path]:
    """Énumère les stems produits par Demucs (``<out>/<model>/<track>/<stem>.wav``)."""
    return {p.stem: p for p in out_dir.rglob("*.wav")}


def separate_stems(wav_path: Path, track_dir: Path, device: str = "cpu"):
    """Lance Demucs puis encode chaque stem en MP3.

    Retourne ``(stems, guitar_wav)`` où ``stems`` est la liste ordonnée des
    noms de pistes exposées au lecteur web.
    """
    stems_dir = track_dir / "stems"
    raw_out = stems_dir / "raw"
    if raw_out.exists():
        shutil.rmtree(raw_out)
    stems_dir.mkdir(parents=True, exist_ok=True)

    model = MODEL_6S
    base = [
        sys.executable, "-m", "demucs",
        "-n", model,
        "-d", device,
        "--out", str(raw_out),
        "--filename", "{stem}.{ext}",
        "--segment", SEGMENT,
        "--overlap", OVERLAP,
        "--shifts", str(SHIFTS),
        str(wav_path),
    ]
    # indice de l'argument "-n <model>" dans `base`
    MODEL_IDX = 4

    def _run_core() -> None:
        _run(base, f"Demucs ({model})")

    try:
        _run_core()
    except Exception as exc:  # noqa: BLE001
        if model == MODEL_6S:
            logger.warning("htdemucs_6s en échec (%s) → repli %s", exc, MODEL_4S)
            model = MODEL_4S
            base[MODEL_IDX] = model
            _run_core()
        else:
            raise

    found = _collect_stems(raw_out)
    if not found:
        raise RuntimeError("Demucs n'a produit aucun stem.")

    # Stem tuilé conservé pour la transcription du solo (guitare, sinon 'other'
    # si repli sur le modèle 4 sources).
    guitar_wav = found.get("guitar") or found.get("other")
    if guitar_wav is not None:
        logger.info("Stem source pour le solo : %s.wav", guitar_wav.stem)

    # Piste « mix » = source complète (utile pour le mixage A/B + le canal).
    _encode_mp3(wav_path, stems_dir / "mix.mp3")

    stems: list[str] = []
    for name in STEM_ORDER:
        src = found.pop(name, None)
        if src is None:
            continue
        _encode_mp3(src, stems_dir / f"{name}.mp3")
        stems.append(name)
        if name not in KEEP_WAV and src != guitar_wav and src.exists():
            src.unlink(missing_ok=True)

    # Stems résiduels (nom sortant du modèle) — on les encode quand même.
    for name, src in found.items():
        _encode_mp3(src, stems_dir / f"{name}.mp3")
        stems.append(name)
        if name not in KEEP_WAV and src != guitar_wav and src.exists():
            src.unlink(missing_ok=True)

    logger.info("Stems encodés (MP3 @%s) : %s", MP3_BITRATE, ", ".join(stems))
    return stems, guitar_wav