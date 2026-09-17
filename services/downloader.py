"""Ingestion : URL YouTube (yt-dlp) ou upload local → audio.wav 44,1 kHz.

Chaque morceau vit dans ``<data_dir>/<id>/source/`` :
  - ``raw.<ext>``   — fichier d'origine (mp3 / webm / m4a …)
  - ``audio.wav``   — piste PCM stéréo normalisée, entrée du pipeline
  - ``audio.mp3``   — mix complet léger, exposé au lecteur web
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

logger = logging.getLogger("guitarlab.downloader")

SR = 44100
MP3_BITRATE = "192k"


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def _to_wav(src: Path, dst: Path) -> Path:
    _ffmpeg(["-i", str(src), "-vn", "-ac", "2", "-ar", str(SR),
             "-c:a", "pcm_s16le", str(dst)])
    return dst


def _to_mp3(src: Path, dst: Path) -> Path:
    _ffmpeg(["-i", str(src), "-vn", "-ac", "2", "-c:a", "libmp3lame",
             "-b:a", MP3_BITRATE, str(dst)])
    return dst


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_youtube_title(url: str) -> "str | None":
    """Récupère le titre d'une URL YouTube sans télécharger l'audio.

    Utilisé en amont du pipeline pour nommer le dossier du morceau.
    Retourne ``None`` si le titre ne peut pas être obtenu.
    """
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("title")
    except Exception:
        return None


def ingest_youtube(url: str, track_dir: Path):
    """Télécharge la meilleure piste audio disponible et la normalise.

    Retourne ``(wav_path, metadata)``.
    """
    source_dir = track_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(source_dir / "yt.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = (info.get("title") or Path(info.get("_filename") or "").stem
             or "Titre inconnu")
    artist = info.get("artist") or info.get("uploader") or "Inconnu"
    duration = float(info.get("duration") or 0.0)
    thumbnail = info.get("thumbnail")

    candidates = sorted(p for p in source_dir.glob("yt.*"))
    raw = next(
        (p for p in candidates
         if p.suffix.lower() in (".mp3", ".webm", ".m4a", ".ogg", ".opus", ".wav")),
        candidates[0] if candidates else None,
    )
    if raw is None:
        raise RuntimeError("yt-dlp n'a produit aucun fichier audio.")

    final_raw = source_dir / f"raw{raw.suffix.lower() or '.mp3'}"
    if raw != final_raw:
        shutil.move(str(raw), str(final_raw))
    for leftover in source_dir.glob("yt.*"):
        leftover.unlink(missing_ok=True)

    wav = _to_wav(final_raw, source_dir / "audio.wav")
    _to_mp3(final_raw, source_dir / "audio.mp3")

    logger.info("Ingestion YouTube OK : %s (%ss)", title, duration)
    return wav, {
        "title": title,
        "artist": artist,
        "duration": round(duration, 2),
        "thumbnail": thumbnail,
        "source": "youtube",
        "source_url": url,
        "created_at": _utc(),
    }


def ingest_upload(raw_path: Path, track_dir: Path):
    """Normalise un fichier audio uploadé.

    ``raw_path`` est le chemin du fichier reçu (déjà écrit par l'API).
    Retourne ``(wav_path, metadata)``.
    """
    source_dir = track_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    if not raw_path.is_file():
        raise FileNotFoundError(f"Fichier introuvable : {raw_path}")

    suff = raw_path.suffix.lower() or ".mp3"
    kept = source_dir / f"raw{suff}"
    if raw_path != kept:
        shutil.move(str(raw_path), str(kept))

    wav = _to_wav(kept, source_dir / "audio.wav")
    _to_mp3(kept, source_dir / "audio.mp3")

    try:
        import soundfile as sf
        duration = float(sf.info(str(wav)).duration)
    except Exception:
        duration = 0.0

    title = raw_path.stem.replace("_", " ").replace("-", " ").strip().title() or "Upload local"
    return wav, {
        "title": title,
        "artist": "Local",
        "duration": round(duration, 2),
        "thumbnail": None,
        "source": "upload",
        "source_url": None,
        "created_at": _utc(),
    }