"""Fournisseur de paroles synchronisées.

Stratégie :
    1. LRCLIB (zero-config, sans clé API) — paroles officielles, quasi instantanées.
    2. Fallback faster-whisper local (français, VAD actif) sur le stem vocal.
"""
from __future__ import annotations
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("guitarlab.lyrics")

UA = "GuitarLab/1.0"
LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
TIMEOUT = 4.0
# Garde-fou sur la transcription Whisper (chargement du modèle + inférence) :
# évite qu'un blocage réseau/modèle ne gèle indéfiniment le pipeline.
TRANSCRIBE_TIMEOUT = float(os.environ.get("TRANSCRIBE_TIMEOUT_SECS", "900"))

from services.lyrics_transcriber import MODELS_DIR  # noqa: E402


def _write_json(path, lyrics) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lyrics, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_with_timeout(func, timeout: float, *args, **kwargs):
    """Exécute ``func(*args)`` dans un thread daemon et attend le résultat.

    Lève ``TimeoutError`` si le délai est dépassé. Le thread est daemon pour
    ne pas bloquer l'arrêt du process (il se termine seul).
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            q.put((True, func(*args, **kwargs)))
        except Exception as exc:  # noqa: BLE001 — propagé via la queue
            q.put((False, exc))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        ok, val = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"{getattr(func, '__name__', 'tâche')} dépassée ({timeout}s)")
    if ok:
        return val
    raise val


# --------------------------------------------------------------------------- #
# 1. Parser LRC -> format interne
# --------------------------------------------------------------------------- #
_LRC_LINE = re.compile(r"^\[(?P<min>\d+):(?P<sec>\d+(?:[.,]\d+)?)\](?P<text>.*)$")


def parse_lrc(lrc: str) -> list[dict]:
    """Parse un fichier LRC et retourne `[{start, end, text}]` en secondes."""
    entries: list[tuple[float, str]] = []
    for line in lrc.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LRC_LINE.match(line)
        if not m:
            continue  # métadonnées [ar:...], [ti:...], etc.
        text = m.group("text").strip()
        if not text:
            continue
        minutes = int(m.group("min"))
        seconds = float(m.group("sec").replace(",", "."))
        entries.append((round(minutes * 60 + seconds, 2), text))
    entries.sort(key=lambda e: e[0])

    lyrics: list[dict] = []
    for i, (start, text) in enumerate(entries):
        if i + 1 < len(entries):
            end = entries[i + 1][0]
            if end - start >= 8:
                end = start + 4.0
        else:
            end = start + 4.0
        lyrics.append({"start": start, "end": round(end, 2), "text": text})
    return lyrics


# --------------------------------------------------------------------------- #
# Nettoyage du titre (suffixes YouTube)
# --------------------------------------------------------------------------- #
_BRACKETED = re.compile(r"[\(\[].*?[\)\]]", re.IGNORECASE)
_KEYWORDS = ("official", "clip", "video", "lyric", "audio", "hd", "4k", "mv", "remastered")


def clean_title(title: str) -> str:
    if not title:
        return ""
    for m in _BRACKETED.finditer(title):
        seg = m.group(0)
        if any(k in seg.lower() for k in _KEYWORDS):
            title = title.replace(seg, " ")
    title = re.sub(r"[\(\)\[\]{}]", " ", title)
    title = re.sub(r"[-–—|]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


# --------------------------------------------------------------------------- #
# 2. LRCLIB (zero-config)
# --------------------------------------------------------------------------- #
def _http_get_json(url: str, params: dict, retries: int = 2) -> Optional[object]:
    """GET JSON avec retries/backoff pour absorber les 503 passagers de LRCLIB."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(full, headers={
            "User-Agent": UA,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                return None  # introuvable, inutile de réessayer
            logger.warning("LRCLIB HTTP %s sur %s (tentative %d/%d)", exc.code, url, attempt + 1, retries + 1)
        except Exception as exc:  # timeout, DNS, hors-ligne…
            logger.warning("LRCLIB indisponible sur %s : %s (tentative %d/%d)", url, exc, attempt + 1, retries + 1)
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    return None


def _get_synced_via_get(clean: str, artist: str, duration: float | None) -> Optional[str]:
    params = {"track_name": clean}
    if artist:
        params["artist_name"] = artist
    if duration:
        params["duration"] = int(duration)
    data = _http_get_json(LRCLIB_GET, params)
    if isinstance(data, dict) and data.get("syncedLyrics"):
        return data["syncedLyrics"]
    return None


def _get_synced_via_search(clean: str, artist: str) -> Optional[str]:
    query = f"{artist} {clean}".strip()
    data = _http_get_json(LRCLIB_SEARCH, {"q": query})
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("syncedLyrics"):
                return item["syncedLyrics"]
    return None


def fetch_lrclib(title: str, artist: str = "", duration: float | None = None) -> list[dict] | None:
    """Récupère les paroles synchronisées LRCLIB, ou None si indisponibles."""
    clean = clean_title(title)
    if not clean:
        return None
    lrc = _get_synced_via_get(clean, artist, duration)
    if not lrc:
        lrc = _get_synced_via_search(clean, artist)
    if not lrc:
        return None
    return parse_lrc(lrc)


# --------------------------------------------------------------------------- #
# 3. Fallback Whisper renforcé
# --------------------------------------------------------------------------- #
def transcribe_vocals_fallback(vocals_path: Path, model_size: str = "small") -> list[dict]:
    """Transcrit le stem vocal en français (VAD actif, anti-hallucinations)."""
    from faster_whisper import WhisperModel
    import torch

    vocals_path = Path(vocals_path)
    if not vocals_path.exists():
        raise FileNotFoundError(f"Piste vocale introuvable : {vocals_path}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    cuda_ok = torch.cuda.is_available() if hasattr(torch, "cuda") else False
    dev, comp = ("cuda", "float16") if cuda_ok else ("cpu", "int8")
    logger.info("faster-whisper fallback (%s) sur %s (%s), cache=%s", model_size, dev, comp, MODELS_DIR)
    try:
        model = WhisperModel(model_size, device=dev, compute_type=comp, download_root=str(MODELS_DIR))
    except Exception as exc:
        if dev == "cuda":
            logger.warning("Échec CUDA (%s), repli CPU int8...", exc)
            model = WhisperModel(model_size, device="cpu", compute_type="int8", download_root=str(MODELS_DIR))
        else:
            raise

    segments_iter, _info = model.transcribe(
        str(vocals_path),
        language="fr",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return [
        {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        for s in segments_iter
        if s.text.strip()
    ]


# --------------------------------------------------------------------------- #
# 4. Orchestrateur
# --------------------------------------------------------------------------- #
def resolve_lyrics(
    title: str,
    artist: str,
    duration: float,
    vocals_path: Path | None,
    output_json: Path,
) -> tuple[list[dict], str]:
    """Résout les paroles : LRCLIB d'abord, sinon Whisper fallback. → (lyrics, source)."""
    lyrics = fetch_lrclib(title, artist, duration)
    if lyrics:
        _write_json(output_json, lyrics)
        return lyrics, "lrclib"

    if vocals_path and Path(vocals_path).exists():
        try:
            # Timeout borné : évite qu'un blocage du modèle (téléchargement ou
            # inférence infinie) ne fige le pipeline.
            lyrics = _run_with_timeout(
                transcribe_vocals_fallback, TRANSCRIBE_TIMEOUT, vocals_path)
            _write_json(output_json, lyrics)
            return lyrics, "whisper"
        except Exception as exc:
            logger.warning("Fallback Whisper échoué pour %s : %s", title, exc)

    return [], "none"
