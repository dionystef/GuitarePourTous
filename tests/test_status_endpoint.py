"""Tests MISSION-SEC-1 : correctif ``GET /api/status/{track_id}``.

Objet du correctif : l'endpoint ne renvoie l'état d'un morceau (200) que si le
morceau est *connu*, c.-à-d. présent dans ``mgr.jobs`` OU avec un
``status.json`` OU un ``metadata.json`` sur disque. Sinon → ``404`` (évite
l'état « queued » fantôme après un ``DELETE``).

Ce fichier ne modifie pas le code métier : il pilote ``main.mgr`` et
``httpx.ASGITransport`` (aucun serveur ni réseau réel).
"""
import asyncio
import json

import main
from tests.helpers import run_app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _reset_mgr():
    """Remet le JobManager dans un état vierge (mémoire + annulation)."""
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()
    main.mgr._cancelled.clear()


def _mk_track(track_id, with_metadata=True, with_status=True,
              metadata_status="ready", status_payload=None):
    """Crée le répertoire ``<DATA_DIR>/<track_id>`` et ses fichiers de vérité."""
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    if with_metadata:
        (d / "metadata.json").write_text(json.dumps({
            "id": track_id, "status": metadata_status, "title": "Titre",
            "artist": "Artiste", "created_at": "2026-10-06T00:00:00Z",
        }), encoding="utf-8")
    if with_status:
        payload = status_payload if status_payload is not None else {
            "id": track_id, "status": "ready", "progress": 100, "logs": [],
        }
        (d / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    return d


def _files_snapshot():
    """Renvoie ``{relpath: (size, mtime_ns)}`` pour tous les fichiers sous DATA_DIR."""
    return {
        str(p.relative_to(main.DATA_DIR)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in main.DATA_DIR.rglob("*") if p.is_file()
    }


# --------------------------------------------------------------------------- #
# Objectif 1 : conformité — 404 inconnu / 200 connu
# --------------------------------------------------------------------------- #
def test_status_unknown_id_returns_404():
    _reset_mgr()

    async def _c(client):
        r = await client.get("/api/status/morceau_inconnu_99999999-999999")
        assert r.status_code == 404, r.text

    run_app(main.app, _c)
    _reset_mgr()


def test_status_known_metadata_returns_200():
    _reset_mgr()
    tid = "connu_meta_1"
    _mk_track(tid, with_metadata=True, with_status=True)

    async def _c(client):
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == tid
        assert body["status"] == "ready"
        assert body["progress"] == 100

    run_app(main.app, _c)
    _reset_mgr()


# --------------------------------------------------------------------------- #
# Objectif 3 : polling après start() → 200 avec état réel
# --------------------------------------------------------------------------- #
def test_status_after_process_start_returns_200(monkeypatch):
    """Après ``POST /api/process`` (→ ``mgr.start()``), le polling immédiat de
    ``GET /api/status/{id}`` doit renvoyer 200 avec un état réel (pas 404)."""
    _reset_mgr()
    # Pipeline neutralisé : on observe uniquement l'état géré par `start`.
    monkeypatch.setattr(main.mgr, "_pipeline", lambda tid, src: None)
    monkeypatch.setattr(main, "fetch_youtube_title", lambda url: "Ma Coloc")

    async def _c(client):
        r = await client.post("/api/process", data={"url": "https://youtu.be/abc"})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        assert tid.startswith("ma-coloc_")
        # Polling immédiat : le morceau est connu via `mgr.jobs`.
        r2 = await client.get(f"/api/status/{tid}")
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["id"] == tid
        assert body["status"] in ("queued", "processing")

    run_app(main.app, _c)
    _reset_mgr()


def test_status_in_memory_job_without_dir_returns_200():
    """Un identifiant présent dans ``jobs`` mais sans répertoire sur disque est
    tout de même « connu » : le polling ne doit pas renvoyer 404."""
    _reset_mgr()
    tid = "mem_job_1"

    async def _c(client):
        main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 42,
                              "logs": ["en cours"]}
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "processing"
        assert r.json()["progress"] == 42

    run_app(main.app, _c)
    _reset_mgr()


# --------------------------------------------------------------------------- #
# Objectif 4 (et 2) : morceau sur disque hors `jobs` (relance après redémarrage)
# --------------------------------------------------------------------------- #
def test_status_track_on_disk_without_jobs_returns_200():
    _reset_mgr()
    tid = "redemarrage_1"
    # État sur disque uniquement (jobs vide = après redémarrage du serveur).
    _mk_track(tid, with_metadata=True, with_status=True)

    async def _c(client):
        assert tid not in main.mgr.jobs
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ready"

    run_app(main.app, _c)
    _reset_mgr()


def test_status_only_status_json_without_metadata_returns_200():
    """`status.json` présent mais `metadata.json` absent (pipeline en cours /
    crash après début de traitement) → 200 avec le statut réel."""
    _reset_mgr()
    tid = "status_only_1"
    _mk_track(tid, with_metadata=False, with_status=True,
              status_payload={"id": tid, "status": "processing", "progress": 55,
                              "logs": ["étape"]})

    async def _c(client):
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "processing"
        assert r.json()["progress"] == 55

    run_app(main.app, _c)
    _reset_mgr()


def test_status_only_metadata_without_status_json_returns_200():
    """`metadata.json` présent mais `status.json` absent → 200 (morceau connu).
    L'état renvoyé est le repli « queued » (aucun `status.json` à lire)."""
    _reset_mgr()
    tid = "meta_only_1"
    _mk_track(tid, with_metadata=True, with_status=False)

    async def _c(client):
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == tid

    run_app(main.app, _c)
    _reset_mgr()


# --------------------------------------------------------------------------- #
# Objectif 2 : cohérence track_detail vs status
# --------------------------------------------------------------------------- #
def test_track_detail_and_status_agree_on_unknown_ids():
    """Pour un identifiant totalement inconnu, les deux endpoints renvoient 404."""
    _reset_mgr()

    async def _c(client):
        for uid in ("inconnu_1", "inconnu_2", "valide_99999999-999999"):
            r_t = await client.get(f"/api/tracks/{uid}")
            r_s = await client.get(f"/api/status/{uid}")
            assert r_t.status_code == 404, f"track_detail({uid})"
            assert r_s.status_code == 404, f"status({uid})"

    run_app(main.app, _c)
    _reset_mgr()


def test_track_detail_and_status_agree_on_completed_track():
    """Pour un morceau terminé (metadata + status sur disque), les deux renvoient 200."""
    _reset_mgr()
    tid = "agree_ok_1"
    _mk_track(tid, with_metadata=True, with_status=True)

    async def _c(client):
        r_t = await client.get(f"/api/tracks/{tid}")
        r_s = await client.get(f"/api/status/{tid}")
        assert r_t.status_code == 200, r_t.text
        assert r_s.status_code == 200, r_s.text

    run_app(main.app, _c)
    _reset_mgr()


# --------------------------------------------------------------------------- #
# Objectif 5 (et 6) : id invalide / traversal → 404 avant accès disque
# --------------------------------------------------------------------------- #
def test_status_rejects_invalid_ids_before_disk_access():
    """Les identifiants capables de sortir de DATA_DIR sont rejetés (404)
    par ``_safe_track_id``, avant tout accès disque. Un identifiant purement
    numérique (bénin) n'échappe pas à DATA_DIR et retombe aussi sur un 404."""
    _reset_mgr()

    async def _c(client):
        for bad in ("../etc", "..", ".", "...", "a\\b", ".hidden", "..abc"):
            r = await client.get(f"/api/status/{bad}")
            assert r.status_code == 404, f"status({bad}) = {r.status_code}"
        # Identifiant purement numérique : bénin (pas de traversal), renvoie 404
        # puisque aucun morceau ne correspond.
        r = await client.get("/api/status/12345")
        assert r.status_code == 404, r.text

    run_app(main.app, _c)
    _reset_mgr()


def test_track_detail_and_status_agree_on_invalid_ids():
    """track_detail et status renvoient les MÊMES codes (404) pour des ids
    invalides / de traversal."""
    _reset_mgr()

    async def _c(client):
        # NB : on n'utilise pas "." (segment courant) : la normalisation d'URL
        # (httpx/Starlette) le fait disparaître, si bien que `/api/tracks/.`
        # aboutit à l'endpoint de LISTE (200) et non à `track_detail`. Ce n'est
        # pas un défaut du correctif status ; on se limite aux ids qui atteignent
        # réellement les deux handlers.
        for bad in ("../etc", "a\\b", ".hidden", "..", "..abc"):
            r_t = await client.get(f"/api/tracks/{bad}")
            r_s = await client.get(f"/api/status/{bad}")
            assert r_t.status_code == 404, f"track_detail({bad})"
            assert r_s.status_code == 404, f"status({bad})"

    run_app(main.app, _c)
    _reset_mgr()


# --------------------------------------------------------------------------- #
# Objectif 4 : pas d'état fantôme pendant le polling après DELETE
# --------------------------------------------------------------------------- #
def test_status_after_delete_returns_404_even_with_running_pipeline(monkeypatch):
    """Même si le pipeline (thread ``to_thread``) est encore en cours, après un
    ``DELETE`` le polling du statut doit renvoyer 404 (pas de « queued » /
    « processing » / « error » fantôme) et ne jamais réécrire ``status.json``."""
    _reset_mgr()

    def _slow_pipeline(track_id, source):
        import time
        time.sleep(0.05)
        # Le thread tente de re-journaliser APRÈS l'annulation : `log` doit
        # ignorer (drapeau ``_cancelled``) pour ne pas recréer status.json.
        main.mgr.log(track_id, "étape post-annulation", 60)
        time.sleep(0.05)

    monkeypatch.setattr(main.mgr, "_pipeline", _slow_pipeline)

    async def _c(client):
        tid = "ghost_poll_1"
        _mk_track(tid, with_metadata=True, with_status=False)
        main.mgr.start(tid, {"type": "upload"})
        await asyncio.sleep(0.05)  # laisse le thread du pipeline démarrer

        r_del = await client.delete(f"/api/tracks/{tid}")
        assert r_del.status_code == 200, r_del.text

        # Polling immédiat après suppression → 404.
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 404, f"juste après DELETE : {r.status_code} {r.text}"

        # Laisse le thread poursuivre puis tenter de réécrire son état.
        await asyncio.sleep(0.25)
        r2 = await client.get(f"/api/status/{tid}")
        assert r2.status_code == 404, f"après fin du thread : {r2.status_code} {r2.text}"
        assert not (main.DATA_DIR / tid / "status.json").exists()
        assert not (main.DATA_DIR / tid / "metadata.json").exists()

    run_app(main.app, _c)
    _reset_mgr()


# --------------------------------------------------------------------------- #
# Objectif « no side-effect » : status n'écrit rien sur disque
# --------------------------------------------------------------------------- #
def test_status_writes_nothing_on_disk():
    """`GET /api/status` pour un morceau connu ne crée/modifie aucun artefact."""
    _reset_mgr()
    tid = "no_side_1"
    _mk_track(tid, with_metadata=True, with_status=True)
    before = _files_snapshot()

    async def _c(client):
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text

    run_app(main.app, _c)
    assert _files_snapshot() == before
    _reset_mgr()


def test_status_unknown_id_creates_nothing_on_disk():
    """Un `GET /api/status` sur un id inconnu (404) ne crée AUCUN fichier."""
    _reset_mgr()
    before = _files_snapshot()

    async def _c(client):
        r = await client.get("/api/status/no_side_inconnu_1")
        assert r.status_code == 404, r.text

    run_app(main.app, _c)
    assert _files_snapshot() == before
    _reset_mgr()
