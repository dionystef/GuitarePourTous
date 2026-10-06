"""Tests d'endpoints MISSION-SEC : non-résurrection après ``DELETE``.

Objectif « GET /api/status/{id} → 404 après DELETE ». Le correctif actuel ne
fait pas encore renvoyer 404 (voir rapport). Ce fichier matérialise le défaut
pour le signaler au dev via la sortie ``@dev``.
"""
import json

import main
from tests.helpers import run_app


def _reset_mgr():
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()
    main.mgr._cancelled.clear()


def _mk_track(track_id, with_metadata=True, with_status=True):
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    if with_metadata:
        (d / "metadata.json").write_text(json.dumps({
            "id": track_id, "status": "ready", "title": "Titre",
            "artist": "Artiste", "created_at": "2026-10-06T00:00:00Z",
        }))
    if with_status:
        (d / "status.json").write_text(json.dumps({"status": "ready"}))


def test_after_delete_status_returns_404():
    """Après ``DELETE /api/tracks/{id}``, ``GET /api/status/{id}`` DOIT renvoyer
    404 (et pas 200 avec un « queued » fantôme).

    Défaut constaté : le endpoint ``status`` retombe sur l'état par défaut
    ``{"status":"queued"}`` → 200. Ce test est volontairement exigeant ; tant
    que le correctif serveur n'est pas appliqué, il échoue.
    """
    _reset_mgr()
    tid = "status_after_delete_1"

    async def _c(client):
        _mk_track(tid)
        r_del = await client.delete(f"/api/tracks/{tid}")
        assert r_del.status_code == 200
        assert r_del.json()["ok"] is True
        # Après suppression : plus de job, artefacts purgés.
        assert tid not in main.mgr.jobs
        assert not (main.DATA_DIR / tid / "status.json").exists()
        # Requis : 404. Le code actuel renvoie 200 (bug MISSION-SEC-1).
        r_status = await client.get(f"/api/status/{tid}")
        assert r_status.status_code == 404, (
            f"GET /api/status/{tid} après DELETE doit renvoyer 404, "
            f"obtenu {r_status.status_code} (corps : {r_status.text})"
        )

    run_app(main.app, _c)
    _reset_mgr()


def test_delete_nonexistent_track_returns_404():
    """Un ``DELETE`` sur un morceau inconnu doit être 404, sans lever d'exception."""
    _reset_mgr()

    async def _c(client):
        r = await client.delete("/api/tracks/inconnu_delete_20261006-120000")
        assert r.status_code == 404

    run_app(main.app, _c)
    _reset_mgr()


def test_delete_invalid_track_id_returns_404():
    """Un identifiant invalide (traversal) est refusé avant tout accès disque."""
    _reset_mgr()

    async def _c(client):
        r = await client.delete("/api/tracks/../etc")
        assert r.status_code == 404

    run_app(main.app, _c)
    _reset_mgr()


def test_delete_removes_track_directory_and_job():
    """Après DELETE, le dossier du morceau et l'état du JobManager sont purgés."""
    _reset_mgr()
    tid = "delete_cleanup_1"

    async def _c(client):
        _mk_track(tid)
        main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 10,
                              "logs": []}
        r = await client.delete(f"/api/tracks/{tid}")
        assert r.status_code == 200
        assert tid not in main.mgr.jobs
        assert not main.DATA_DIR.joinpath(tid).exists()

    run_app(main.app, _c)
    _reset_mgr()
