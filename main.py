"""Guitar Lab — API REST FastAPI.

Pipeline asynchrone par morceau :
    ingestion (YouTube / upload) → séparation Demucs → grille d'accords →
    transcription du solo (basic-pitch).

État & logs exposés via ``GET /api/status/{id}`` (polling) et
``WS /api/ws/{id}`` (temps réel). Le frontend statique est servi directement.

Résolution du device : ``DEVICE=auto`` → CUDA si disponible, sinon CPU.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
                                 ingest_youtube)
from services.library import delete_track as delete_library_track
from services.separator import separate_stems
from services.solo_transcriber import transcribe_solo

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("guitarlab")

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", HERE / "static"))
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024

DATA_DIR.mkdir(parents=True, exist_ok=True)


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
    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.listeners: dict[str, set[WebSocket]] = {}
        self.jobs: dict[str, dict] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # --- statut ---------------------------------------------------------
    def _status_obj(self, track_id: str) -> dict:
        found = self.jobs.get(track_id)
        if found is not None:
            return found
        disk = DATA_DIR / track_id / "status.json"
        if disk.exists():
            try:
                return json.loads(disk.read_text("utf-8"))
            except Exception:
                pass
        return {"id": track_id, "status": "queued", "progress": 0, "logs": []}

    def snapshot(self, track_id: str) -> dict:
        return dict(self._status_obj(track_id))

    def log(self, track_id: str, message: str, progress: float,
            status: str = "processing") -> None:
        st = self._status_obj(track_id)
        st["status"] = status
        st["progress"] = int(round(progress))
        st.setdefault("logs", []).append(str(message))
        st["logs"] = st["logs"][-250:]
        self.jobs[track_id] = st
        try:
            (DATA_DIR / track_id / "status.json").write_text(
                json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        self.emit(track_id, {"type": "status", "status": self.snapshot(track_id)})

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
        # Nouveau traitement : état et logs repartent de zéro.
        self.jobs[track_id] = {"id": track_id, "status": "queued", "progress": 0, "logs": []}
        self.log(track_id, "Tâche mise en file.", 1)
        loop = self.loop or asyncio.get_running_loop()
        loop.create_task(self._job(track_id, source))

    async def _job(self, track_id: str, source: dict) -> None:
        try:
            await asyncio.to_thread(self._pipeline, track_id, source)
        except Exception:  # noqa: BLE001 — défensif
            logger.exception("Tâche %s interrompue", track_id)
            self.log(track_id, "✖ Tâche interrompue (erreur inattendue).",
                     100, status="error")

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

            # 4) SOLO (MIS EN PAUSE TEMPORAIREMENT - ÉCONOMIE DE RESSOURCES)
            # self.log(track_id, "Transcription du solo (basic-pitch)…", 86)
            # solo_wav = guitar_wav or wav
            # midi_path, tab_path, nb_notes = transcribe_solo(
            #     solo_wav, track_dir, analysis["bpm"])
            # self.log(track_id, f"✔ Solo extrait : {nb_notes} notes → "
            #                    f"{midi_path.name} + {tab_path.name}", 96)

            # 5) FINALISATION
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
            self.log(track_id, "✔ Terminé. Bonne répétition ! 🎸", 100, status="ready")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pipeline en échec pour %s", track_id)
            self.log(track_id, f"✖ Erreur : {exc}", 100, status="error")
            try:
                (track_dir / "metadata.json").write_text(json.dumps({
                    "id": track_id,
                    "status": "error",
                    "error": str(exc)[:600],
                    "source": source,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass


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
# Application FastAPI
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_app: FastAPI):
    mgr.bind(asyncio.get_running_loop())
    logger.info("Guitar Lab prêt — data=%s device=%s", DATA_DIR, DEVICE)
    yield


app = FastAPI(title="Guitar Lab", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def range_file_response(path: Path, request: Request):
    stat = path.stat()
    file_size = stat.st_size
    range_header = request.headers.get("range")
    media_type = guess_type(str(path))[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    }
    if not range_header or not range_header.startswith("bytes="):
        headers["Content-Length"] = str(file_size)
        return FileResponse(path, headers=headers, media_type=media_type)
    try:
        range_val = range_header.strip().split("=")[-1]
        start_str, end_str = range_val.split("-")
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        content_length = end - start + 1
        headers.update({
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
        })

        def iterfile():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = content_length
                chunk_size = 64 * 1024
                while remaining > 0:
                    read_size = min(chunk_size, remaining)
                    chunk = f.read(read_size)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(iterfile(), status_code=206, headers=headers, media_type=media_type)
    except Exception:
        headers["Content-Length"] = str(file_size)
        return FileResponse(path, headers=headers, media_type=media_type)


@app.get("/data/{file_path:path}")
async def serve_data_file(file_path: str, request: Request):
    path = (DATA_DIR / file_path).resolve()
    if not path.is_file() or not str(path).startswith(str(DATA_DIR.resolve())):
        raise HTTPException(404, "Fichier introuvable.")
    return range_file_response(path, request)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "device": DEVICE, "data_dir": str(DATA_DIR)}


@app.get("/api/tracks")
def tracks():
    return list_tracks()


@app.get("/api/tracks/{track_id}")
def track_detail(track_id: str):
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
    return detail


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: str):
    if not delete_library_track(track_id):
        raise HTTPException(404, "Morceau inconnu.")
    mgr.jobs.pop(track_id, None)
    return {"ok": True, "id": track_id, "message": "Morceau supprimé."}


class StructureUpdate(BaseModel):
    line_breaks: list[int] = []
    sections: dict[str, str] = {}  # e.g. {"1": "Intro", "5": "Couplet", "21": "Refrain"}


@app.post("/api/tracks/{track_id}/structure")
async def update_structure(track_id: str, request: Request):
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau introuvable.")
    try:
        body = await request.json()
        meta = json.loads(meta_path.read_text("utf-8"))
        if "line_breaks" in body:
            meta["line_breaks"] = [int(x) for x in body["line_breaks"]]
        if "sections" in body:
            meta["sections"] = {str(k): str(v) for k, v in body["sections"].items()}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "line_breaks": meta.get("line_breaks", []), "sections": meta.get("sections", {})}
    except Exception as exc:
        raise HTTPException(500, f"Erreur: {exc}")


@app.post("/api/tracks/{track_id}/bar-offset")
async def set_bar_offset(track_id: str, offset: int = Form(...)):
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau introuvable.")
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        meta["bar_offset"] = int(offset) % 4
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "bar_offset": meta["bar_offset"]}
    except Exception as exc:
        raise HTTPException(500, f"Erreur: {exc}")


@app.post("/api/tracks/{track_id}/structure-start")
async def set_structure_start(track_id: str, measure: int = Form(...)):
    meta_path = DATA_DIR / track_id / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "Morceau introuvable.")
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        meta["structure_start"] = int(measure)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "structure_start": meta["structure_start"]}
    except Exception as exc:
        raise HTTPException(500, f"Erreur: {exc}")


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
        title = Path(safe_name).stem
        track_id = _track_id_for(title)
        raw = DATA_DIR / track_id / "source"
        raw.mkdir(parents=True, exist_ok=True)
        contents = await file.read()
        if len(contents) > MAX_UPLOAD:
            raise HTTPException(413, "Fichier trop volumineux (500 Mo max).")
        dest = raw / safe_name
        dest.write_bytes(contents)
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
    return mgr.snapshot(track_id)


@app.websocket("/api/ws/{track_id}")
async def ws_status(ws: WebSocket, track_id: str):
    await ws.accept()
    mgr.add_listener(track_id, ws)
    try:
        await ws.send_json({"type": "status",
                            "status": mgr.snapshot(track_id)})
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        mgr.remove_listener(track_id, ws)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)