"""Tests MISSION-01 : reprise automatique des décortications en arrière-plan.

Cible ``JobManager.active_jobs()`` et l'endpoint ``GET /api/jobs/active`` :
  * liste uniquement les statuts ``queued`` / ``processing`` ;
  * lecture protégée par ``self._lock`` (aucune lecture de ``jobs`` hors verrou) ;
  * aucune résurrection d'état fantôme (les morceaux sur disque hors ``jobs``
    — ex. après redémarrage — ne sont PAS listés) ;
  * non-régression sur DELETE : un job supprimé/cancellé ne réapparaît pas,
    même si le thread ``to_thread`` tourne encore.

Aucune modification du code métier : on pilote ``main.mgr`` et ``httpx``
(transport ASGI, sans réseau réel).
"""
import asyncio
import json

import main
from tests.helpers import run_app


def _reset():
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()
    main.mgr._cancelled.clear()


def _mk_status_json(track_id, status="processing", progress=50):
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({
        "id": track_id, "status": status, "progress": progress, "logs": [],
    }), encoding="utf-8")


def _mk_metadata_json(track_id, status="ready"):
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps({
        "id": track_id, "status": status, "title": "Titre",
        "artist": "Artiste", "created_at": "2026-10-06T00:00:00Z",
    }), encoding="utf-8")


# --------------------------------------------------------------------------- #
# active_jobs() : filtrage par statut
# --------------------------------------------------------------------------- #
def test_active_jobs_empty_when_no_jobs():
    _reset()
    assert main.mgr.active_jobs() == []
    _reset()


def test_active_jobs_returns_only_queued_and_processing():
    _reset()
    main.mgr.jobs["j_queued"] = {"id": "j_queued", "status": "queued", "progress": 0, "logs": []}
    main.mgr.jobs["j_proc"] = {"id": "j_proc", "status": "processing", "progress": 40, "logs": []}
    main.mgr.jobs["j_ready"] = {"id": "j_ready", "status": "ready", "progress": 100, "logs": []}
    main.mgr.jobs["j_err"] = {"id": "j_err", "status": "error", "progress": 100, "logs": []}
    main.mgr.jobs["j_unknown"] = {"id": "j_unknown", "status": "unknown", "progress": 0, "logs": []}

    active = main.mgr.active_jobs()
    ids = {j["id"] for j in active}
    assert ids == {"j_queued", "j_proc"}
    # L'état renvoyé est une copie, sans logs/état mutables partagés.
    for j in active:
        assert j["status"] in ("queued", "processing")
    _reset()


def test_active_jobs_returns_copies_not_references():
    """Les états renvoyés sont des copies : réassigner une clé scalaire du
    résultat n'altère pas l'état interne de ``jobs``."""
    _reset()
    main.mgr.jobs["j_copy"] = {"id": "j_copy", "status": "processing", "progress": 30, "logs": ["x"]}
    [j] = main.mgr.active_jobs()
    j["status"] = "ready"
    j["progress"] = 99
    assert main.mgr.jobs["j_copy"]["status"] == "processing"
    assert main.mgr.jobs["j_copy"]["progress"] == 30
    _reset()


def test_active_jobs_reads_under_lock(monkeypatch):
    """La lecture de ``jobs`` sous ``self._lock`` : on observe l'état du verrou
    au moment où ``items()`` est itéré."""
    _reset()
    tid = "j_lock"
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 5, "logs": []}
    observed = {}

    class _SpyDict(dict):
        def items(self):
            observed["locked"] = main.mgr._lock.locked()
            return super().items()

    monkeypatch.setattr(main.mgr, "jobs", _SpyDict(main.mgr.jobs))
    active = main.mgr.active_jobs()
    assert observed.get("locked") is True
    assert any(j["id"] == tid for j in active)
    _reset()


# --------------------------------------------------------------------------- #
# active_jobs() : absence de résurrection / cohérence avec snapshot_known
# --------------------------------------------------------------------------- #
def test_active_jobs_does_not_resurrect_disk_only_jobs():
    """Un morceau présent sur disque (metadata/status) mais absent de ``jobs``
    (ex. redémarrage) n'est PAS listé : ``active_jobs`` ne lit que la mémoire."""
    _reset()
    tid = "disk_only_1"
    _mk_metadata_json(tid, status="ready")
    _mk_status_json(tid, status="processing", progress=55)
    assert main.mgr.active_jobs() == []
    # Cohérence : snapshot_known le connaît (200) mais il n'est pas « actif ».
    assert main.mgr.snapshot_known(tid)["status"] == "processing"
    _reset()


def test_active_jobs_excludes_cancelled_after_remove():
    """Après cancel+remove (ordre exact du DELETE), le job disparaît de la
    liste active et ne réapparaît pas malgré le drapeau ``_cancelled``."""
    _reset()
    tid = "cancel_rm_1"
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 30, "logs": []}
    main.mgr.cancel_track(tid)
    main.mgr.remove_job(tid)
    assert main.mgr.is_cancelled(tid) is True
    assert main.mgr.active_jobs() == []
    _reset()


def test_active_jobs_after_delete_with_running_thread(monkeypatch):
    """Même si le thread ``to_thread`` tourne encore et tente de re-journaliser
    après annulation, le job ne réapparaît pas dans ``active_jobs``."""
    _reset()
    tid = "del_thread_1"
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 10, "logs": []}

    def _late_logger(track_id, source):
        import time
        time.sleep(0.05)
        # Le thread poursuit puis tente de reloger après l'annulation.
        main.mgr.log(track_id, "étape tardive", 60)
        time.sleep(0.05)

    monkeypatch.setattr(main.mgr, "_pipeline", _late_logger)

    async def _run():
        main.mgr.loop = asyncio.get_running_loop()
        main.mgr.start(tid, {"type": "upload"})
        await asyncio.sleep(0.02)
        # Ordre exact du DELETE :
        main.mgr.cancel_track(tid)
        main.mgr.remove_job(tid)
        await asyncio.sleep(0.2)  # laisse le thread tenter de reloger
        assert main.mgr.active_jobs() == []
        assert tid not in main.mgr.jobs
        await main.mgr.shutdown()

    asyncio.run(_run())
    _reset()


def test_active_jobs_excludes_timed_out_error():
    """Un job passé en ``error`` n'est plus « actif » (pas de reprise)."""
    _reset()
    tid = "err_1"
    main.mgr.jobs[tid] = {"id": tid, "status": "error", "progress": 100, "logs": []}
    assert main.mgr.active_jobs() == []
    _reset()


# --------------------------------------------------------------------------- #
# Endpoint GET /api/jobs/active
# --------------------------------------------------------------------------- #
def test_jobs_active_endpoint_empty_returns_200():
    _reset()

    async def _c(client):
        r = await client.get("/api/jobs/active")
        assert r.status_code == 200, r.text
        assert r.json() == []

    run_app(main.app, _c)
    _reset()


def test_jobs_active_endpoint_returns_active_jobs(monkeypatch):
    """Avec un job démarré (state en mémoire), l'endpoint renvoie 200 avec la
    liste des actifs."""
    _reset()
    monkeypatch.setattr(main.mgr, "_pipeline", lambda tid, src: None)

    async def _c(client):
        main.mgr.start("endpoint_active_1", {"type": "upload"})
        r = await client.get("/api/jobs/active")
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(j["id"] == "endpoint_active_1" for j in body), body
        # Cohérence : /api/status renvoie aussi le job (200).
        tid = "endpoint_active_1"
        rs = await client.get(f"/api/status/{tid}")
        assert rs.status_code == 200
        await main.mgr.shutdown()

    run_app(main.app, _c)
    _reset()


def test_jobs_active_endpoint_empty_after_delete():
    """Après DELETE, l'endpoint ne liste plus le job (404 sur status)."""
    _reset()
    tid = "endpoint_del_1"
    _mk_metadata_json(tid, status="ready")
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 10, "logs": []}

    async def _c(client):
        r = await client.delete(f"/api/tracks/{tid}")
        assert r.status_code == 200, r.text
        ra = await client.get("/api/jobs/active")
        assert ra.status_code == 200
        assert all(j["id"] != tid for j in ra.json()), ra.json()
        rs = await client.get(f"/api/status/{tid}")
        assert rs.status_code == 404

    run_app(main.app, _c)
    _reset()


def test_jobs_active_endpoint_multiple_active_jobs():
    """Plusieurs jobs actifs : l'endpoint renvoie tous les actifs (le frontend
    reprend alors le premier)."""
    _reset()
    main.mgr.jobs["multi_a"] = {"id": "multi_a", "status": "processing", "progress": 20, "logs": []}
    main.mgr.jobs["multi_b"] = {"id": "multi_b", "status": "queued", "progress": 0, "logs": []}
    main.mgr.jobs["multi_ready"] = {"id": "multi_ready", "status": "ready", "progress": 100, "logs": []}

    async def _c(client):
        r = await client.get("/api/jobs/active")
        assert r.status_code == 200
        ids = {j["id"] for j in r.json()}
        assert ids == {"multi_a", "multi_b"}

    run_app(main.app, _c)
    _reset()
