"""Guitar Lab — API REST FastAPI.

Pipeline asynchrone par morceau :
    ingestion (YouTube / upload) → séparation Demucs → grille d'accords →
    transcription du solo (basic-pitch).

État & logs exposés via ``GET /api/status/{id}`` (polling) et
``WS /api/ws/{id}`` (temps réel). Le frontend statique est servi directement.

Résolution du device : ``DEVICE=auto`` → CUDA si disponible, sinon CPU.
"""
from __future__ import annotations

import multiprocessing

# PyInstaller + multiprocessing (torch/demucs) : évite les sous-processus
# orphelins qui relancent le binaire et font planter uvicorn.
multiprocessing.freeze_support()

import anyio
import asyncio
import json
import logging
import os
import re
import sys
import threading
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import formatdate
from mimetypes import guess_type
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.chord_analyzer import analyze as analyze_harmony
from services.downloader import (fetch_youtube_title, ingest_upload,
                                 ingest_youtube, is_supported_audio,
                                 is_supported_audio_by_ext,
                                 validate_youtube_url)
from services.library import _safe_track_id, delete_track as delete_library_track
from services.lyrics_provider import resolve_lyrics
from services.separator import separate_stems
from services.solo_transcriber import transcribe_solo

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("guitarlab")

HERE = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    # PyInstaller (mode --onedir) : toutes les données embarquées vivent
    # dans le dossier `_MEIPASS` (services, static, …).
    HERE = Path(getattr(sys, "_MEIPASS", HERE))

# En binaire autonome, les données utilisateur vont dans un emplacement
# inscriptible et persistant ; en dev on garde `HERE/data`.
_default_data = (Path.home() / ".local" / "share" / "guitarlab" / "data"
                 if getattr(sys, "frozen", False) else HERE / "data")
# Assainit la valeur DATA_DIR : sous Windows, le préfixe verbeux `\\?\` (issu de
# la résolution du chemin par le wrapper Tauri) déclenche `[Errno 22] Invalid
# argument` sur les API de fichiers. On le supprime pour obtenir un chemin
# standard (ex. `C:\...`).
_data_dir_env = os.environ.get("DATA_DIR")
if not _data_dir_env:
    _data_dir_env = str(_default_data)
elif _data_dir_env.startswith("\\\\?\\"):
    _data_dir_env = _data_dir_env[4:]
DATA_DIR = Path(_data_dir_env)
STATIC_DIR = Path(os.environ.get("STATIC_DIR", HERE / "static"))
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
CHUNK_UPLOAD = 1024 * 1024  # lecture du corps d'upload par tranches de 1 Mo
# Garde-fou sur la durée totale d'une tâche (Demucs/whisper peuvent être longs) :
# évite qu'un thread bloqué ne reste orphelin indéfiniment.
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECS", "3600"))
# Limite de taille de frame WebSocket (l'app n'envoie que des « ping »).
WS_MAX_SIZE = int(os.environ.get("WS_MAX_SIZE", str(4 * 1024 * 1024)))

DATA_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Version de l'application (tag Git, commit, ou repli)
# --------------------------------------------------------------------------- #
def get_git_version() -> str:
    """Retourne le tag Git courant ou le commit abrégé, avec repli propre."""
    # 1. Variable d'environnement explicite si définie
    if os.environ.get("APP_VERSION"):
        return os.environ["APP_VERSION"]
    # 2. Exécution de git describe
    try:
        import subprocess
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--always"],
            cwd=str(HERE),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if tag:
            return tag
    except Exception:
        pass
    # 3. Repli par défaut
    return "v1.3.0"


APP_VERSION = get_git_version()


# --------------------------------------------------------------------------- #
# Device de calcul (fallback CPU automatique)
# --------------------------------------------------------------------------- #
def resolve_device() -> str:
    requested = os.environ.get("DEVICE", "auto").strip().lower()
    try:
        import torch
        cuda = bool(torch.cuda.is_available())
    except Exception:
        cuda = False

    if requested in ("", "auto"):
        return "cuda" if cuda else "cpu"
    if requested in ("cuda", "gpu"):
        if not cuda:
            logger.warning("DEVICE=%s demandé mais aucun GPU CUDA détecté "
                           "→ bascule automatique sur CPU", requested)
            return "cpu"
        return "cuda"
    return requested if requested in ("cpu", "mps") else "cpu"


DEVICE = resolve_device()
logger.info("⚡ Device de calcul : %s", DEVICE)


# --------------------------------------------------------------------------- #
# Orchestrateur du pipeline (thread de fond + diffusion en direct)
# --------------------------------------------------------------------------- #
class JobManager:
    """Orchestrateur du pipeline.

    ``jobs`` est lu/écrit depuis la boucle asyncio (endpoints) ET depuis les
    threads ``to_thread`` (pipeline) : un verrou protège donc chaque accès
    pour éviter les courses. ``tasks`` est, lui, exclusivement manipulé sur la
    boucle (annulation DELETE/shutdown).
    """

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.listeners: dict[str, set[WebSocket]] = {}
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._lock = threading.Lock()
        # Identifiants dont le traitement a été annulé (DELETE / timeout).
        # Le thread ``to_thread`` n'étant pas interrompable, ce drapeau lui
        # permet de s'auto-supprimer plutôt que de ré-écrire des artefacts.
        self._cancelled: set[str] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # --- statut ---------------------------------------------------------
    def _load_job_locked(self, track_id: str) -> dict:
        """Retourne l'état du job (mémoire ou disque). À appeler verrou tenu."""
        st = self.jobs.get(track_id)
        if st is not None:
            return st
        disk = DATA_DIR / track_id / "status.json"
        if disk.exists():
            try:
                return json.loads(disk.read_text("utf-8"))
            except Exception:
                pass
        # Repli : un `metadata.json` présent atteste que le morceau existe (au
        # moins finalisé/relancé après redémarrage). On en dérive l'état au lieu
        # d'inventer un « queued » en dur, ce qui évite de mentir sur le statut.
        meta = DATA_DIR / track_id / "metadata.json"
        if meta.exists():
            try:
                m = json.loads(meta.read_text("utf-8"))
                return {
                    "id": track_id,
                    "status": m.get("status", "unknown"),
                    "progress": 100 if m.get("status") == "ready" else 0,
                    "logs": [],
                }
            except Exception:
                pass
        return {"id": track_id, "status": "queued", "progress": 0, "logs": []}

    def snapshot(self, track_id: str) -> dict:
        with self._lock:
            return dict(self._load_job_locked(track_id))

    def snapshot_known(self, track_id: str) -> Optional[dict]:
        """Retourne l'état du morceau s'il est *connu*, sinon ``None``.

        Le contrôle de connaissance et la lecture de l'état sont effectués sous
        un unique verrou (atomicité). Un morceau est « connu » s'il est présent
        dans ``jobs``, ou si un ``status.json`` ou un ``metadata.json`` existe
        sur le disque. Ainsi, un identifiant inconnu ou déjà supprimé ne renvoie
        jamais l'état par défaut « queued » (qui masquerait la suppression).
        """
        with self._lock:
            known = (
                track_id in self.jobs
                or (DATA_DIR / track_id / "status.json").exists()
                or (DATA_DIR / track_id / "metadata.json").exists()
            )
            if not known:
                return None
            st = self._load_job_locked(track_id)
            # TOCTOU : un DELETE concurrent (rmtree sans `_lock`) peut retirer les
            # artefacts entre le contrôle « connu » et la lecture. `_load_job_locked`
            # retombe alors sur le défaut « queued » qui masquerait la suppression.
            if (
                st.get("status") == "queued"
                and track_id not in self.jobs
                and not (DATA_DIR / track_id / "status.json").exists()
                and not (DATA_DIR / track_id / "metadata.json").exists()
            ):
                return None
            return dict(st)

    def active_jobs(self) -> list[dict]:
        """Retourne les décortications actives (statut ``queued`` ou ``processing``).

        Permet au frontend de reprendre automatiquement le suivi d'un job après
        un rechargement de page ou une reconnexion. La lecture est protégée par
        ``self._lock`` : ``jobs`` est mis à jour depuis la boucle asyncio ET les
        threads ``to_thread``.
        """
        with self._lock:
            return [
                dict(st)
                for track_id, st in self.jobs.items()
                if st.get("status") in ("queued", "processing")
            ]

    def log(self, track_id: str, message: str, progress: float,
            status: str = "processing") -> None:
        with self._lock:
            # Un morceau annulé ne doit plus écrire son état (mémoire + disque) :
            # le thread ``to_thread`` continuerait sinon à recréer les artefacts
            # après le DELETE, malgré l'annulation de la coroutine.
            if track_id in self._cancelled:
                return
            st = self._load_job_locked(track_id)
            st["status"] = status
            st["progress"] = int(round(progress))
            st.setdefault("logs", []).append(str(message))
            st["logs"] = st["logs"][-250:]
            self.jobs[track_id] = st
            snap = dict(st)
        try:
            (DATA_DIR / track_id / "status.json").write_text(
                json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        self.emit(track_id, {"type": "status", "status": snap})

    # --- websockets ------------------------------------------------------
    def add_listener(self, track_id: str, ws: WebSocket) -> None:
        self.listeners.setdefault(track_id, set()).add(ws)

    def remove_listener(self, track_id: str, ws: WebSocket) -> None:
        self.listeners.get(track_id, set()).discard(ws)

    def emit(self, track_id: str, event: dict) -> None:
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(track_id, event), self.loop)

    async def _broadcast(self, track_id: str, event: dict) -> None:
        for ws in list(self.listeners.get(track_id, ())):
            try:
                await ws.send_json(event)
            except Exception:
                self.remove_listener(track_id, ws)

    # --- lancement --------------------------------------------------------
    def start(self, track_id: str, source: dict) -> None:
        # Nouveau traitement : état, logs et éventuel drapeau d'annulation
        # résiduel repartent de zéro.
        with self._lock:
            self._cancelled.discard(track_id)
            self.jobs[track_id] = {"id": track_id, "status": "queued", "progress": 0, "logs": []}
        self.log(track_id, "Tâche mise en file.", 1)
        loop = self.loop or asyncio.get_running_loop()
        self.tasks[track_id] = loop.create_task(self._job(track_id, source))

    def cancel_track(self, track_id: str) -> None:
        """Annule la tâche asyncio associée à un morceau, si elle est en cours."""
        with self._lock:
            self._cancelled.add(track_id)
        task = self.tasks.pop(track_id, None)
        if task is not None and not task.done():
            task.cancel()

    def is_cancelled(self, track_id: str) -> bool:
        """Indique si un traitement a été annulé (lecture côté thread pipeline)."""
        with self._lock:
            return track_id in self._cancelled

    def remove_job(self, track_id: str) -> None:
        """Purge l'état d'un morceau (mémoire + ``status.json`` disque).

        Ne retire pas l'identifiant de ``_cancelled`` : le thread ``to_thread``
        encore en cours doit rester « annulé » afin de ne pas recréer les
        artefacts après suppression.
        """
        with self._lock:
            self.jobs.pop(track_id, None)
        try:
            (DATA_DIR / track_id / "status.json").unlink(missing_ok=True)
        except OSError:
            pass

    async def shutdown(self) -> None:
        """Annule proprement toutes les tâches en cours (arrêt du serveur)."""
        pending = [t for t in self.tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _job(self, track_id: str, source: dict) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._pipeline, track_id, source),
                timeout=JOB_TIMEOUT,
            )
        except asyncio.CancelledError:
            logger.info("Tâche %s annulée", track_id)
            # Ne pas ré-écrire l'état via self.log() : ce dernier relit
            # status.json depuis le disque (fallback _load_job_locked) puis le
            # ré-écrit, ce qui ressuscite le job en mémoire après le DELETE.
            # On purge simplement l'état mémoire + disque du morceau annulé.
            self.remove_job(track_id)
            raise
        except asyncio.TimeoutError:
            logger.error("Tâche %s abandonnée (timeout %ss)", track_id, JOB_TIMEOUT)
            self.log(track_id, "✖ Tâche abandonnée (délai dépassé).", 100, status="error")
        except Exception:  # noqa: BLE001 — défensif
            logger.exception("Tâche %s interrompue", track_id)
            self.log(track_id, "✖ Tâche interrompue (erreur inattendue).",
                     100, status="error")
        finally:
            self.tasks.pop(track_id, None)

    # --- pipeline -----------------------------------------------------------
    def _pipeline(self, track_id: str, source: dict) -> None:
        track_dir = DATA_DIR / track_id
        track_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.log(track_id, f"▶ Pipeline démarré — source : {source.get('type')}", 4)

            # 1) INGESTION
            if source["type"] == "youtube":
                self.log(track_id, "Téléchargement du flux audio YouTube…", 8)
                wav, meta = ingest_youtube(source["url"], track_dir)
            else:
                self.log(track_id, f"Réception de « {source['filename']} »…", 8)
                wav, meta = ingest_upload(source["raw_path"], track_dir)
            self.log(track_id,
                     f"✔ Ingestion : « {meta['title']} » ({meta['duration']} s)", 22)

            # 2) STEMS
            self.log(track_id, "Séparation de sources (Demucs htdemucs_6s)… "
                               "peut prendre plusieurs minutes…", 28)
            stems, guitar_wav = separate_stems(wav, track_dir, DEVICE)
            self.log(track_id, f"✔ Stems générés : {', '.join(stems)}", 60)

            # 3) ACCORDS
            self.log(track_id, "Analyse harmonique (battements + accords)…", 66)
            analysis = analyze_harmony(wav)
            (track_dir / "chords.json").write_text(
                json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
            self.log(
                track_id,
                f"✔ Grille d'accords : {analysis['bpm']:.0f} BPM, "
                f"{len(analysis['bars'])} mesures", 82)

            # 3 bis) PAROLES
            vocals_path = track_dir / "stems" / "vocals.mp3"
            self.log(track_id, "Recherche des paroles synchronisées (LRCLIB)…", 83)
            lyrics, src = resolve_lyrics(
                meta["title"],
                meta.get("artist", ""),
                meta.get("duration", 0),
                vocals_path,
                track_dir / "lyrics.json",
            )
            if src == "lrclib":
                self.log(track_id, f"✔ Paroles officielles synchronisées ({len(lyrics)} phrases)", 92)
            elif src == "whisper":
                self.log(track_id, f"✔ Paroles transcrites par IA ({len(lyrics)} phrases)", 92)
            else:
                self.log(track_id, "ℹ Aucune parole trouvée pour ce morceau", 92)

            # 4) SOLO (MIS EN PAUSE TEMPORAIREMENT - ÉCONOMIE DE RESSOURCES)
            # self.log(track_id, "Transcription du solo (basic-pitch)…", 86)
            # solo_wav = guitar_wav or wav
            # midi_path, tab_path, nb_notes = transcribe_solo(
            #     solo_wav, track_dir, analysis["bpm"])
            # self.log(track_id, f"✔ Solo extrait : {nb_notes} notes → "
            #                    f"{midi_path.name} + {tab_path.name}", 96)

            # 5) FINALISATION
            # Le thread ``to_thread`` n'est pas interrompable : si le morceau
            # a été annulé pendant le traitement, on abandonne ici plutôt que
            # d'écrire un `metadata.json` « prêt » sur un morceau supprimé.
            if self.is_cancelled(track_id):
                logger.info("Pipeline %s annulé en fin de traitement : "
                            "finalisation abandonnée", track_id)
                return
            files_dict = {
                "wav": f"/data/{track_id}/source/audio.wav",
                "mp3": f"/data/{track_id}/source/audio.mp3",
                "chords": f"/data/{track_id}/chords.json",
            }
            # Réactiver lors du retour du solo :
            # if 'midi_path' in locals() and midi_path:
            #     files_dict["midi"] = f"/data/{track_id}/{midi_path.name}"
            #     files_dict["tab"] = f"/data/{track_id}/{tab_path.name}"
            meta.update({
                "id": track_id,
                "status": "ready",
                "bpm": round(float(analysis["bpm"]), 1),
                "beats_per_bar": analysis["beats_per_bar"],
                "time_signature": analysis["time_signature"],
                "stems": stems,
                "files": files_dict,
            })
            (track_dir / "metadata.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            self._cleanup_original(track_dir)
            self.log(track_id, "✔ Terminé. Bonne répétition ! 🎸", 100, status="ready")
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            logger.exception("Pipeline en échec pour %s : %s", track_id, exc)
            frames = [line.strip() for line in tb.splitlines() if "File " in line]
            last_frame = f" [{frames[-1]}]" if frames else ""
            err_msg = str(exc) or exc.__class__.__name__
            self.log(track_id, f"✖ Échec du traitement : {err_msg}{last_frame}", 100, status="error")
            try:
                (track_dir / "crash.log").write_text(tb, encoding="utf-8", errors="ignore")
                (DATA_DIR / "last_crash.log").write_text(tb, encoding="utf-8", errors="ignore")
            except Exception:
                pass
            # Un morceau annulé ne doit pas recréer de `metadata.json` d'erreur.
            if self.is_cancelled(track_id):
                return
            try:
                (track_dir / "metadata.json").write_text(json.dumps({
                    "id": track_id,
                    "status": "error",
                    "error": "Erreur interne survenue pendant le traitement.",
                    "source": source.get("type", "unknown"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    @staticmethod
    def _cleanup_original(track_dir: Path) -> None:
        """Supprime le fichier source original volumineux une fois le pipeline
        terminé (séparation + accords), afin de préserver l'espace disque.

        Conserve : ``source/audio.wav``, ``source/audio.mp3``, les stems et
        ``metadata.json``. Supprime : ``source/raw.*`` (fichier téléchargé
        YouTube ou fichier uploadé).
        """
        source_dir = track_dir / "source"
        if not source_dir.is_dir():
            return
        try:
            for p in source_dir.glob("raw.*"):
                size_mb = p.stat().st_size / (1024 * 1024)
                p.unlink(missing_ok=True)
                logger.info(
                    "Nettoyage : %s supprimé (%.1f Mo d'espace libéré)",
                    p.name, size_mb,
                )
        except Exception:
            logger.debug("Nettoyage du fichier source ignoré", exc_info=True)


mgr = JobManager()


# --------------------------------------------------------------------------- #
# Identifiants lisibles : <slug-du-titre>_<yyyyMMdd-HHmmss>
# --------------------------------------------------------------------------- #
def _slugify(text: str, maxlen: int = 60) -> str:
    """Transforme un titre en nom de dossier sûr (ASCII, minuscules, tirets)."""
    s = unicodedata.normalize("NFKD", str(text or "")) \
        .encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:maxlen].strip("-") or "titre"


def _track_id_for(title: str) -> str:
    """Construit l'id de morceau : ``<slug>_<timestamp>`` (collés)."""
    slug = _slugify(title)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug}_{ts}"


def _existing_by_source_url(url: str) -> "dict | None":
    """Retourne le ``metadata`` d'un morceau déjà issu de cette URL YouTube."""
    for meta_path in DATA_DIR.glob("*/metadata.json"):
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            continue
        if meta.get("source_url") == url and meta.get("id"):
            return meta
    return None


# --------------------------------------------------------------------------- #
# Bibliothèque locale
# --------------------------------------------------------------------------- #
def _summary(meta: dict) -> dict:
    files = meta.get("files") or {}
    return {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "artist": meta.get("artist"),
        "folder": meta.get("folder"),
        "duration": meta.get("duration"),
        "bpm": meta.get("bpm"),
        "status": meta.get("status", "unknown"),
        "source": meta.get("source"),
        "thumbnail": meta.get("thumbnail"),
        "created_at": meta.get("created_at"),
        "stems": meta.get("stems", []),
        "files": files,
        "bar_offset": meta.get("bar_offset", 0),
        "structure_start": meta.get("structure_start"),
        "line_breaks": meta.get("line_breaks", []),
        "sections": meta.get("sections", {}),
    }


def list_tracks() -> list[dict]:
    rows = []
    for meta_path in DATA_DIR.glob("*/metadata.json"):
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            continue
        rows.append(_summary(meta))
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# Dossiers virtuels de la bibliothèque
# --------------------------------------------------------------------------- #
def _folders_file() -> Path:
    """Chemin du fichier JSON qui persiste la liste des dossiers créés par
    l'utilisateur (``<DATA_DIR>/folders.json``)."""
    return DATA_DIR / "folders.json"


def _load_folders() -> list[str]:
    """Retourne les noms de dossiers persistés dans ``folders.json``.

    Fichier absents, corrompu ou de forme inattendue → liste vide (repli sûr).
    """
    p = _folders_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    if isinstance(data, dict):
        # Tolérance : un objet ``{"Rock": true}`` est accepté en cas d'évolution
        # du format, on ne conserve que les clés non vides.
        return [str(k).strip() for k in data if str(k).strip()]
    return []


def _save_folders(folders: list[str]) -> None:
    """Persiste la liste ordonnée des dossiers dans ``folders.json``."""
    normalized = [str(f).strip() for f in folders if str(f).strip()]
    # Écriture atomique : on n'expose jamais un fichier à moitié écrit.
    tmp = _folders_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_folders_file())


def _write_metadata(meta_path: Path, meta: dict) -> None:
    """Écrit un ``metadata.json`` de façon atomique (tmp + replace).

    Évite qu'une écriture interrompue (crash, coupure) ne laisse un fichier
    tronqué qui ferait considérer le morceau comme corrompu par ``_summary``/GET.
    """
    tmp = meta_path.with_name(meta_path.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(meta_path)


# Noms réservés aux onglets système du frontend (Tous / Non classés) : un
# dossier utilisateur portant ce nom rendrait le filtrage ambigu.
_RESERVED_FOLDER_NAMES = {"all", "unclassified"}


def _is_reserved_folder_name(name: str) -> bool:
    """Retourne ``True`` si un nom de dossier doit être écarté de la liste.

    Deux cas sont couverts, quelle que soit la provenance (``folders.json`` ou
    données de métadonnées légacy) :
      * les noms réservés aux onglets système (``all`` / ``unclassified``,
        insensibles à la casse) ;
      * le préfixe ``__``, espace de noms interne des clés d'onglets du frontend
        (``__all__`` / ``__none__``).
    """
    n = str(name).strip()
    return n.lower() in _RESERVED_FOLDER_NAMES or n.startswith("__")


def _validate_folder_name(name: str) -> str:
    """Valide et nettoie un nom de dossier NON VIDE.

    Lève ``HTTPException(400)`` si le nom est vide, trop long, réservé, contient
    des caractères de contrôle ou un séparateur de chemin (``/`` ou ``\\``).
    Ces derniers sont interdits car le nom est utilisé comme segment de route
    pour ``DELETE /api/folders/{name}`` : un ``/`` cassèrait le routage.

    Les noms ``all`` et ``unclassified`` (insensibles à la casse) sont réservés :
    ils ne doivent jamais entrer en collision avec les onglets « Tous » /
    « Non classés » du frontend.
    """
    cleaned = str(name).strip()
    if not cleaned:
        raise HTTPException(400, "Nom de dossier vide.")
    if len(cleaned) > 100:
        raise HTTPException(400, "Nom de dossier trop long (100 caractères max).")
    if any(ord(c) < 32 for c in cleaned):
        raise HTTPException(400, "Nom de dossier invalide (caractères de contrôle).")
    if "/" in cleaned or "\\" in cleaned:
        raise HTTPException(400, "Nom de dossier invalide (le caractère « / » n'est pas autorisé).")
    if cleaned.lower() in _RESERVED_FOLDER_NAMES:
        raise HTTPException(400, "Nom de dossier réservé (onglet système).")
    # Le préfixe « __ » est l'espace de noms interne des onglets système du
    # frontend (__all__ / __none__) : aucun dossier utilisateur ne doit
    # l'utiliser, sans quoi la logique de filtre dépendrait d'une équality
    # avec une chaîne utilisateur incontrôlée.
    if cleaned.startswith("__"):
        raise HTTPException(400, "Nom de dossier invalide (préfixe réservé « __ »).")
    return cleaned


def _tracks_folders_from_meta() -> set[str]:
    """Consolide les dossiers réellement déclarés dans les ``metadata.json``.

    Un dossier peut exister sans être dans ``folders.json`` (ex. assignation
    directe via le sélecteur de carte) : il doit donc apparaître dans la liste.

    Les noms réservés (``all``/``unclassified``, insensibles à la casse) et le
    préfixe ``__`` (espace interne du frontend) sont ignorés : ce sont des
    valeurs corrompues ou historiques qui ne doivent jamais créer d'onglet
    ambigu avec les clés système.
    """
    names: set[str] = set()
    for meta_path in DATA_DIR.glob("*/metadata.json"):
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            continue
        folder = str(meta.get("folder") or "").strip()
        if not folder or _is_reserved_folder_name(folder):
            continue
        names.add(folder)
    return names


# --------------------------------------------------------------------------- #
# Application FastAPI
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_app: FastAPI):
    mgr.bind(asyncio.get_running_loop())
    logger.info("Guitar Lab prêt — data=%s device=%s", DATA_DIR, DEVICE)
    yield
    logger.info("Arrêt : annulation des tâches en cours…")
    await mgr.shutdown()


app = FastAPI(title="Guitar Lab", version="1.0.0", lifespan=lifespan)


# CSP : autorise les inline scripts/styles (script d'init du thème + attributs
# `style=`) présents dans les pages statiques, tout en gardant `default-src 'self'`.
# `'unsafe-inline'` reste nécessaire pour ce frontend non-remodelé ; la protection
# XSS principale est l'échappement côté client (`esc`) et la validation serveur.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://i.ytimg.com; "
    "media-src 'self' blob:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def disable_static_cache_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    # En-têtes de sécurité appliqués à toutes les réponses.
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/data/{file_path:path}")
async def serve_data_file(file_path: str, request: Request):
    path = (DATA_DIR / file_path).resolve()
    if not path.is_file() or not path.is_relative_to(DATA_DIR.resolve()):
        raise HTTPException(404, "Fichier introuvable.")
    stat = path.stat()
    file_size = stat.st_size
    media_type = guess_type(str(path))[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    }

    # Support des requêtes HTTP Range (seeking audio) : 206 + Content-Range,
    # sinon 200 + fichier complet.
    start, end = 0, file_size - 1
    status_code = 200
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            range_val = range_header.strip().split("=")[-1]
            start_str, end_str = range_val.split("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
            start = max(0, min(start, file_size - 1))
            end = max(start, min(end, file_size - 1))
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            status_code = 206
        except Exception:
            start, end, status_code = 0, file_size - 1, 200
    headers["Content-Length"] = str(end - start + 1)

    async def iterfile():
        # anyio.open_file lit de façon asynchrone → ne bloque pas la boucle
        # d'événements Uvicorn, même quand les 6 stems sont demandés en
        # parallèle.
        f = await anyio.open_file(path, "rb")
        try:
            await f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = await f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            try:
                await f.aclose()
            except Exception:
                pass

    return StreamingResponse(iterfile(), status_code=status_code,
                             headers=headers, media_type=media_type)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "device": DEVICE,
    }


@app.get("/api/tracks")
def tracks():
    return list_tracks()


@app.get("/api/tracks/{track_id}")
def track_detail(track_id: str):
    if not _safe_track_id(track_id):
        raise HTTPException(404, "Morceau inconnu.")
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau inconnu.")
    meta = json.loads(meta_path.read_text("utf-8"))
    detail = _summary(meta)
    chords_path = DATA_DIR / track_id / "chords.json"
    if chords_path.exists():
        try:
            detail["chords"] = json.loads(chords_path.read_text("utf-8"))
        except Exception:
            detail["chords"] = None
    lyrics_path = DATA_DIR / track_id / "lyrics.json"
    if lyrics_path.exists():
        try:
            detail["lyrics"] = json.loads(lyrics_path.read_text("utf-8"))
        except Exception:
            detail["lyrics"] = None
    return detail


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: str):
    if not _safe_track_id(track_id) or not delete_library_track(track_id):
        raise HTTPException(404, "Morceau inconnu.")
    # Annule la tâche asyncio en cours (le thread `to_thread` ne peut pas être
    # interrompu, mais la boucle cesse de l'attendre et son état est purgé).
    mgr.cancel_track(track_id)
    mgr.remove_job(track_id)
    return {"ok": True, "id": track_id, "message": "Morceau supprimé."}


class StructureUpdate(BaseModel):
    line_breaks: list[int] = []
    sections: dict[str, str] = {}  # e.g. {"1": "Intro", "5": "Couplet", "21": "Refrain"}


class TrackMetaUpdate(BaseModel):
    """Payload de mise à jour des métadonnées d'un morceau (titre/artiste/dossier).

    Chaque champ est optionnel : seuls les champs explicitement fournis sont
    modifiés. ``None`` (champ absent) = ne pas toucher ; ``""`` = effacer.
    """

    title: Optional[str] = None
    artist: Optional[str] = None
    folder: Optional[str] = None


@app.patch("/api/tracks/{track_id}")
async def update_track_meta(track_id: str, request: Request):
    """Met à jour ``title`` / ``artist`` / ``folder`` d'un morceau.

    - Les trois champs sont nettoyés via ``.strip()`` ;
    - un ``folder`` vide est stocké comme ``None`` (non classé) ;
    - le titre ne peut pas devenir vide après nettoyage (400).

    Le corps JSON est validé via le modèle :class:`TrackMetaUpdate` (chaque
    champ est optionnel ; un champ absent = on n'y touche pas).
    """
    if not _safe_track_id(track_id):
        raise HTTPException(404, "Morceau inconnu.")
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau inconnu.")
    try:
        body = await request.json()
        update = TrackMetaUpdate.model_validate(body)
    except Exception as exc:
        raise HTTPException(400, f"Payload de mise à jour invalide : {exc}")
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        if update.title is not None:
            title = update.title.strip()
            if not title:
                raise HTTPException(400, "Le titre ne peut pas être vide.")
            meta["title"] = title
        if update.artist is not None:
            # Un artiste vide est accepté (l'utilisateur peut vouloir l'effacer),
            # mais on stocke une chaîne nettoyée plutôt qu'un blanc.
            meta["artist"] = update.artist.strip()
        if update.folder is not None:
            folder_raw = update.folder.strip()
            # Un dossier vide (ou blanc) = on déclasse le morceau.
            meta["folder"] = _validate_folder_name(folder_raw) if folder_raw else None
        _write_metadata(meta_path, meta)
        return _summary(meta)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Mise à jour des métadonnées en échec pour %s", track_id)
        raise HTTPException(500, "Erreur interne.")


# --------------------------------------------------------------------------- #
# Dossiers virtuels : liste consolidée, création et suppression
# --------------------------------------------------------------------------- #
@app.get("/api/folders")
def list_folders():
    """Liste consolidée et triée des dossiers de la bibliothèque.

    Combine ``folders.json`` (dossiers créés explicitement par l'utilisateur)
    et les dossiers déclarés dans les ``metadata.json`` des morceaux (assignation
    directe via le sélecteur de carte). Renvoie un tableau de noms triés.

    Les noms réservés (``all``/``unclassified``, insensibles à la casse) et le
    préfixe ``__`` (espace interne du frontend) sont écartés des deux sources :
    un ``folders.json`` historique peut contenir de tels noms créés avant la
    réservation, et ils ne doivent jamais créer d'onglet ambigu.
    """
    names = {n for n in _load_folders() if not _is_reserved_folder_name(n)}
    names |= _tracks_folders_from_meta()
    return sorted(names)


@app.post("/api/folders")
def create_folder(name: str = Form("")):
    """Crée un nouveau dossier et le persiste dans ``folders.json``."""
    cleaned = _validate_folder_name(name)
    folders = _load_folders()
    if cleaned not in folders:
        folders.append(cleaned)
        _save_folders(folders)
    return {"ok": True, "name": cleaned, "folders": sorted(set(folders))}


@app.delete("/api/folders/{name}")
def delete_folder(name: str):
    """Supprime un dossier de ``folders.json`` et déclasse ses morceaux.

    Tous les morceaux dont ``metadata.folder`` correspond à ce nom repassent à
    ``folder = None`` (non classés), afin qu'aucune carte ne pointe vers un
    dossier disparu.
    """
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(400, "Nom de dossier vide.")
    folders = _load_folders()
    if cleaned in folders:
        _save_folders([f for f in folders if f != cleaned])
    # Décasse systématiquement les morceaux affectés (même s'ils ne figuraient
    # pas dans folders.json) : source de vérité = métadonnées des morceaux.
    removed = 0
    for meta_path in DATA_DIR.glob("*/metadata.json"):
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            continue
        if str(meta.get("folder") or "").strip() == cleaned:
            meta["folder"] = None
            _write_metadata(meta_path, meta)
            removed += 1
    return {
        "ok": True,
        "name": cleaned,
        "removed_tracks": removed,
        "folders": sorted(_load_folders()),
    }


@app.post("/api/tracks/{track_id}/structure")
async def update_structure(track_id: str, request: Request):
    if not _safe_track_id(track_id):
        raise HTTPException(404, "Morceau introuvable.")
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau introuvable.")
    try:
        body = await request.json()
        meta = json.loads(meta_path.read_text("utf-8"))
        if "line_breaks" in body:
            # Les numéros de mesure sont des entiers positifs : on refuse toute
            # valeur inattendue (au lieu de laisser une 500 opaque).
            breaks = []
            for x in body["line_breaks"]:
                try:
                    n = int(x)
                except (TypeError, ValueError):
                    raise HTTPException(400, "Saut de ligne invalide.")
                if n < 0:
                    raise HTTPException(400, "Saut de ligne invalide.")
                breaks.append(n)
            meta["line_breaks"] = breaks
        if "sections" in body:
            sections = {}
            for k, v in body["sections"].items():
                try:
                    measure = int(k)
                except (TypeError, ValueError):
                    raise HTTPException(400, "Étiquette de section invalide (numéro de mesure attendu).")
                if measure <= 0:
                    raise HTTPException(400, "Étiquette de section invalide (numéro de mesure attendu).")
                label = str(v).strip()
                if not label:
                    raise HTTPException(400, "Étiquette de section vide.")
                if len(label) > 80:
                    raise HTTPException(400, "Étiquette de section trop longue.")
                # Bloque caractères de contrôle (évite l'injection de métadonnées).
                if any(ord(c) < 32 for c in label):
                    raise HTTPException(400, "Étiquette de section invalide.")
                sections[str(measure)] = label
            meta["sections"] = sections
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "line_breaks": meta.get("line_breaks", []), "sections": meta.get("sections", {})}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Mise à jour de la structure en échec pour %s", track_id)
        raise HTTPException(500, "Erreur interne.")


@app.post("/api/tracks/{track_id}/bar-offset")
async def set_bar_offset(track_id: str, offset: int = Form(...)):
    if not _safe_track_id(track_id):
        raise HTTPException(404, "Morceau introuvable.")
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau introuvable.")
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        meta["bar_offset"] = int(offset) % 4
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "bar_offset": meta["bar_offset"]}
    except Exception:
        logger.exception("Réglage du bar-offset en échec pour %s", track_id)
        raise HTTPException(500, "Erreur interne.")


@app.post("/api/tracks/{track_id}/structure-start")
async def set_structure_start(track_id: str, measure: int = Form(...)):
    if not _safe_track_id(track_id):
        raise HTTPException(404, "Morceau introuvable.")
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau introuvable.")
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        meta["structure_start"] = int(measure)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "structure_start": meta["structure_start"]}
    except Exception:
        logger.exception("Réglage du structure-start en échec pour %s", track_id)
        raise HTTPException(500, "Erreur interne.")


@app.post("/api/process")
async def process(url: Optional[str] = Form(None),
                  file: Optional[UploadFile] = File(None)):
    """Lance le traitement d'une URL YouTube OU d'un fichier audio uploadé.

    L'identifiant de morceau (et donc le dossier dans ``data/``) est construit
    comme ``<slug-du-titre>_<yyyyMMdd-HHmmss>`` afin de reconnaître d'un coup
    d'œil à quelle chanson correspond chaque dossier.
    """
    if url and url.strip().startswith(("http://", "https://")):
        url = url.strip()
        # SSRF : seules les URL YouTube (youtube.com / youtu.be + sous-domaines)
        # sont acceptées ; les hôtes IP et résolutions privées sont rejetés.
        if not validate_youtube_url(url):
            raise HTTPException(
                400,
                "URL non autorisée : seules les URL YouTube "
                "(youtube.com, youtu.be) sont acceptées.",
            )

        # Déjà traité pour cette même URL ? → on rouvre l'existant.
        existing = _existing_by_source_url(url)
        if existing:
            meta_path = DATA_DIR / existing["id"] / "metadata.json"
            meta = json.loads(meta_path.read_text("utf-8"))
            return {"id": existing["id"], "status": meta.get("status", "unknown"),
                    "duplicate": True}

        title = await asyncio.to_thread(fetch_youtube_title, url)
        track_id = _track_id_for(title or "youtube")
        source = {"type": "youtube", "url": url}
    elif file and file.filename:
        safe_name = Path(file.filename).name or "audio.mp3"
        # M7 : allowlist d'extensions avant toute écriture disque.
        if not is_supported_audio_by_ext(safe_name):
            raise HTTPException(415, "Format audio non supporté.")
        title = Path(safe_name).stem
        track_id = _track_id_for(title)
        raw = DATA_DIR / track_id / "source"
        raw.mkdir(parents=True, exist_ok=True)
        dest = raw / safe_name
        # E4 : ne pas lire tout le corps en mémoire. On écrit par tranches avec
        # un cap (MAX_UPLOAD + 1 octet) puis rejet 413 dès que le seuil est franchi.
        try:
            total = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = await file.read(CHUNK_UPLOAD)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "Fichier trop volumineux (500 Mo max).")
                    out.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        # M7 : vérification du contenu (magic bytes) → 415 si non audio.
        if not is_supported_audio(dest):
            dest.unlink(missing_ok=True)
            raise HTTPException(415, "Fichier audio invalide ou contenu non reconnu.")
        source = {"type": "upload", "filename": safe_name, "raw_path": dest}
    else:
        raise HTTPException(400, "Fournissez `url` (YouTube) ou `file` (audio).")

    meta_path = DATA_DIR / track_id / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text("utf-8"))
        return {"id": track_id, "status": meta.get("status", "unknown"),
                "duplicate": True}

    mgr.start(track_id, source)
    return {"id": track_id, "status": "queued"}


@app.get("/api/status/{track_id}")
def status(track_id: str):
    if not _safe_track_id(track_id):
        raise HTTPException(404, "Morceau inconnu.")
    # MISSION-SEC-1 : le contrôle de connaissance et la lecture de l'état sont
    # atomiques (un seul verrou via `snapshot_known`). Un morceau inconnu ou
    # supprimé renvoie `None` → 404, sans jamais révéler d'état fantôme « queued ».
    st = mgr.snapshot_known(track_id)
    if st is None:
        raise HTTPException(404, "Morceau inconnu.")
    return st


@app.get("/api/jobs/active")
def active_jobs():
    """Liste des décortications actives (statut ``queued`` ou ``processing``).

    Consommé par le frontend pour reprendre automatiquement le suivi d'un
    décorticage après un rechargement de page ou une reconnexion."""
    return mgr.active_jobs()


@app.websocket("/api/ws/{track_id}")
async def ws_status(ws: WebSocket, track_id: str):
    # M8 : valider l'identifiant avant tout accès disque (anti path traversal).
    if not _safe_track_id(track_id):
        await ws.close(code=1008)
        return
    # MISSION-SEC-1 (b) : même garde « connu » que l'endpoint HTTP /status,
    # afin d'éviter toute divergence HTTP/WS (socket fermée pour un identifiant
    # inconnu ou déjà supprimé, au lieu d'envoyer un état « queued » fantôme).
    st = mgr.snapshot_known(track_id)
    if st is None:
        await ws.close(code=1008)
        return
    await ws.accept()
    mgr.add_listener(track_id, ws)
    try:
        await ws.send_json({"type": "status", "status": st})
        while True:
            msg = await ws.receive_text()
            # Garde-fou : ne pas accepter de trames démesurées (ping attendu).
            if len(msg) > 4096:
                await ws.close(code=1009)
                break
            if msg == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        mgr.remove_listener(track_id, ws)


if __name__ == "__main__":
    # E1 : par défaut, écoute uniquement sur le loopback (pas d'exposition réseau).
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    # On passe l'objet `app` (pas la chaîne "main:app") : indispensable pour
    # que uvicorn fonctionne dans le binaire PyInstaller.
    uvicorn.run(app, host=host, port=port, reload=False, ws_max_size=WS_MAX_SIZE)