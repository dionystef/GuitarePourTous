"""Ingestion : URL YouTube (yt-dlp) ou upload local → audio.wav 44,1 kHz.

Chaque morceau vit dans ``<data_dir>/<id>/source/`` :
  - ``raw.<ext>``   — fichier d'origine (mp3 / webm / m4a …)
  - ``audio.wav``   — piste PCM stéréo normalisée, entrée du pipeline
  - ``audio.mp3``   — mix complet léger, exposé au lecteur web
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

logger = logging.getLogger("guitarlab.downloader")

SR = 44100
MP3_BITRATE = "192k"

# Timeout réseau pour les échanges yt-dlp (évite les téléchargements bloqués).
SOCKET_TIMEOUT = float(os.environ.get("YTDLP_SOCKET_TIMEOUT", "30"))

# M7 : extensions audio autorisées pour l'upload (pipeline ffmpeg/soundfile).
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".webm"}


def _clean_windows_path(path: str) -> str:
    """Retire le préfixe Windows verbeux d'un chemin.

    Sous Windows, certains chemins résolus par l'app portent un préfixe
    verbeux (deux antislashs, un point d'interrogation, un antislash) comme
    ``\\\\?\\``, incompatible avec certains appels système de FFmpeg, ce qui
    provoque ``[Errno 22] Invalid argument``. Seules les chaînes réellement
    préfixées sont réécrites (les options CLI comme ``-ac`` ne sont jamais
    affectées).
    """
    return path[4:] if path.startswith("\\\\?\\") else path


def _ffmpeg(args: list[str]) -> None:
    # Assainit aussi le binaire (path de shutil.which) : un préfixe `\\?\`
    # résiduel rendrait le spawn du processus impossible sous Windows.
    bin_ffmpeg = _clean_windows_path(shutil.which("ffmpeg") or "ffmpeg")
    # Assainit les chemins avant l'exécution (voir _clean_windows_path) : un
    # préfixe `\\?\` résiduel ferait échouer FFmpeg avec [Errno 22].
    cleaned = [_clean_windows_path(arg) for arg in args]
    creation_flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.run(
        [bin_ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *cleaned],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creation_flags,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"FFmpeg ({proc.returncode}): {tail}")


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


# --------------------------------------------------------------------------- #
# Gardes d'URL + SSRF
# --------------------------------------------------------------------------- #
_YT_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*(?:youtube\.com|youtu\.be)$", re.IGNORECASE)
_IP_LITERAL_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def validate_youtube_url(url: str) -> bool:
    """Valide qu'une URL correspond à un hôte YouTube autorisé.

    Réponse ``False`` pour les hôtes hors allowlist, les IP littérales,
    les identifiants embarqués (user:pass@) et les schémas non http(s).
    """
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if not host or not _YT_HOST_RE.match(host):
        return False
    # IP littérale (ex. 1.2.3.4) ou IPv6 : pas un hôte YouTube légitime.
    if _IP_LITERAL_RE.match(host) or ":" in host:
        return False
    return True


def _is_private_or_local_ip(ip_str: str) -> bool:
    """Vrai si l'adresse appartient à un réseau privé/link-local/… (anti-SSRF)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _reject_non_public(url: str) -> None:
    """Lève ``RuntimeError`` si l'URL résout vers une adresse non publique.

    Appelée dans les fonctions d'ingestion (exécutées dans un thread) : la
    résolution DNS bloquante ne gèle donc pas la boucle asyncio.
    """
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return
    # IP littérale privée → rejet immédiat.
    if _is_private_or_local_ip(host):
        raise RuntimeError("URL YouTube vers une adresse privée refusée.")
    # Résolution DNS : on vérifie l'ensemble des adresses retournées.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        logger.warning("Résolution DNS impossible pour %s : %s", host, exc)
        return
    for info in infos:
        if _is_private_or_local_ip(info[4][0]):
            raise RuntimeError("URL YouTube résolvant vers une adresse privée refusée.")


# --------------------------------------------------------------------------- #
# Validation du contenu audio uploadé (extension + magic bytes)
# --------------------------------------------------------------------------- #
def is_supported_audio_by_ext(name: str) -> bool:
    """Vrai si ``name`` porte une extension audio de la allowlist."""
    return Path(name).suffix.lower() in ALLOWED_AUDIO_EXT


def _has_audio_magic(path: Path) -> bool:
    """Détecte les signatures binaires attendues (mp3/wav/flac/ogg/mp4/webm)."""
    try:
        with open(path, "rb") as f:
            header = f.read(12)
    except OSError:
        return False
    if not header:
        return False
    # MP3 : ID3 ou frame MPEG (0xFF 0xEx/0xFx)
    if header.startswith(b"ID3") or (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
        return True
    # WAV : "RIFF"...."WAVE"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return True
    # FLAC
    if header.startswith(b"fLaC"):
        return True
    # OGG / Opus
    if header.startswith(b"OggS"):
        return True
    # MP4 / M4A : 'ftyp' à l'offset 4
    if header[4:8] == b"ftyp":
        return True
    # Matroska / WebM : EBML 0x1A45DFA3
    if header[0:4] == b"\x1a\x45\xdf\xa3":
        return True
    # AAC (ADTS) : 0xFFFx
    if header[0] == 0xFF and (header[1] & 0xF6) == 0xF0:
        return True
    return False


def is_supported_audio(path: Path) -> bool:
    """Vrai si l'extension ET la signature binaire correspondent à un audio."""
    if not is_supported_audio_by_ext(path.name):
        return False
    return _has_audio_magic(path)


def _youtube_match_filter(info, *args):
    """N'accepte que les résultats issus de l'extracteur YouTube.

    Équivalent côté yt-dlp d'une désactivation de l'extracteur générique :
    toute entrée non-YouTube est refusée (retourne la raison du refus).
    """
    key = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if key.startswith("youtube"):
        return None
    return "URL non YouTube refusée (extracteur générique désactivé)."


def _youtube_ydl_opts(**extra) -> dict:
    """Options yt-dlp communes (timeout réseau + anti-generic)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "match_filter": _youtube_match_filter,
        # Sécurise le nommage des fichiers téléchargés : `windowsfilenames`
        # force une compatibilité Windows (caractères réservés, points finaux),
        # `restrictfilenames` restreint aux caractères ASCII. Les deux évitent
        # les noms de sortie invalides pour les liens suivants du pipeline.
        "windowsfilenames": True,
        "restrictfilenames": True,
    }
    bin_ffmpeg = shutil.which("ffmpeg")
    if bin_ffmpeg:
        opts["ffmpeg_location"] = str(Path(bin_ffmpeg).parent)
    opts.update(extra)
    return opts


def fetch_youtube_title(url: str) -> "str | None":
    """Récupère le titre d'une URL YouTube sans télécharger l'audio.

    Utilisé en amont du pipeline pour nommer le dossier du morceau.
    Retourne ``None`` si l'URL est invalide/privée ou si le titre échoue.
    """
    # Garde SSRF : l'URL doit être issue de la allowlist YouTube et résoudre
    # vers une adresse publique (mitigation DNS rebinding côté yt-dlp).
    if not validate_youtube_url(url):
        return None
    try:
        _reject_non_public(url)
    except RuntimeError as exc:
        logger.warning("URL YouTube refusée : %s", exc)
        return None
    try:
        with yt_dlp.YoutubeDL(_youtube_ydl_opts(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("title")
    except Exception:
        return None


def _download_thumbnail(url: str, dest: Path) -> None:
    """Télécharge la pochette locale (best-effort, ne fait jamais échouer l'ingestion)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = resp.read()
        if data:
            dest.write_bytes(data)
            logger.info("Pochette enregistrée : %s", dest.name)
    except Exception as exc:  # noqa: BLE001 — la miniature reste optionnelle
        logger.warning("Échec du téléchargement de la miniature %s : %s", url, exc)


def ingest_youtube(url: str, track_dir: Path):
    """Télécharge la meilleure piste audio disponible et la normalise.

    Retourne ``(wav_path, metadata)``.
    """
    if not validate_youtube_url(url):
        raise ValueError(
            "URL YouTube non valide : seules les URL youtube.com / youtu.be "
            "sont acceptées."
        )
    _reject_non_public(url)
    source_dir = track_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    opts = _youtube_ydl_opts(
        format="bestaudio/best",
        outtmpl=str(source_dir / "yt.%(ext)s"),
        postprocessors=[{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Détection d'un flux « playlist / album » (cas typique YouTube Music) : on
    # extrait les métadonnées de la première entrée valide plutôt que du wrapper.
    entries = info.get("entries")
    entry = next((e for e in entries if e), None) if entries else None
    if entry is None:
        entry = info  # vidéo simple : les métadonnées sont au niveau racine

    title = (entry.get("title") or info.get("title")
             or Path(info.get("_filename") or "").stem or "Titre inconnu")
    artist = (entry.get("artist") or entry.get("uploader")
              or info.get("artist") or info.get("uploader") or "Inconnu")
    duration = float(entry.get("duration") or info.get("duration") or 0.0)
    thumbnail = entry.get("thumbnail") or info.get("thumbnail")
    if not thumbnail:
        thumb_entry = entry.get("thumbnails") or []
        thumb_info = info.get("thumbnails") or []
        if thumb_entry:
            thumbnail = thumb_entry[-1].get("url")
        elif thumb_info:
            thumbnail = thumb_info[-1].get("url")

    # Téléchargement local de la pochette (best-effort) : la miniature échouant
    # ne doit jamais faire échouer l'ingestion. Le chemin servi correspond à
    # l'exposition ``/data/<id>/…`` du backend.
    if thumbnail:
        _download_thumbnail(thumbnail, track_dir / "thumbnail.jpg")
    local_thumbnail = f"/data/{track_dir.name}/thumbnail.jpg" \
        if (track_dir / "thumbnail.jpg").exists() else None

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
        "thumbnail": local_thumbnail,
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
    # Défense en profondeur (l'API valide déjà) : extension + magic bytes.
    if not is_supported_audio(raw_path):
        raise ValueError(f"Fichier audio non reconnu : {raw_path.name}")

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