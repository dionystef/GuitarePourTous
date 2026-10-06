"""Tests du JobManager : verrou, annulation (DELETE) et arrêt (shutdown).

Le pipeline lourd est remplacé par un stub ``_pipeline`` qui dort, afin de
pouvoir observer les états du verrou / des tâches sans side-effects réels.
"""
import asyncio

import main


def _bind_loop():
    loop = asyncio.get_running_loop()
    main.mgr.loop = loop
    return loop


def _sleep_pipeline(track_id, source, delay=0.2):
    import time
    time.sleep(delay)


def _reset():
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()
    main.mgr._pipeline = _sleep_pipeline


def test_snapshot_defaults_to_queued():
    _reset()
    snap = main.mgr.snapshot("inconnu")
    assert snap["status"] == "queued"
    assert snap["progress"] == 0


def test_start_creates_job_and_task():
    _reset()

    async def _run():
        _bind_loop()
        main.mgr.start("job_a", {"type": "upload"})
        await asyncio.sleep(0.05)
        assert "job_a" in main.mgr.jobs
        assert "job_a" in main.mgr.tasks

    asyncio.run(_run())
    _reset()


def test_cancel_track_pops_task():
    _reset()

    async def _run():
        _bind_loop()
        main.mgr.start("job_b", {"type": "upload"})
        await asyncio.sleep(0.05)
        main.mgr.cancel_track("job_b")
        await asyncio.sleep(0.3)
        assert "job_b" not in main.mgr.tasks

    asyncio.run(_run())
    _reset()


def test_remove_job_removes_memory_state():
    _reset()

    async def _run():
        _bind_loop()
        main.mgr.start("job_c", {"type": "upload"})
        await asyncio.sleep(0.05)
        main.mgr.remove_job("job_c")
        assert "job_c" not in main.mgr.jobs
        # Snapshot relit le disque par défaut (nouveau job "queued").
        main.mgr.jobs.pop("job_c", None)

    asyncio.run(_run())
    _reset()


def test_shutdown_cancels_pending_and_clears_tasks():
    _reset()

    async def _run():
        _bind_loop()
        main.mgr.start("job_d", {"type": "upload"})
        await asyncio.sleep(0.05)
        await main.mgr.shutdown()
        assert "job_d" not in main.mgr.tasks

    asyncio.run(_run())
    _reset()


def test_after_delete_track_not_resurrected():
    """Régression attendue : après DELETE (cancel+remove), le job ne doit PAS
    réapparaître en mémoire ni réécrire status.json sur le disque.

    Le correctif actuel ré-ajoute le job via le handler ``CancelledError`` de
    ``_job`` (``self.log(..., status='error')``). Ce test matérialise le défaut.
    """
    import json
    _reset()
    tid = "job_probe"
    (main.DATA_DIR / tid).mkdir(parents=True, exist_ok=True)
    (main.DATA_DIR / tid / "status.json").write_text(json.dumps({"status": "processing"}))

    async def _run():
        _bind_loop()
        main.mgr.start(tid, {"type": "upload"})
        await asyncio.sleep(0.05)
        # Ordre exact de l'endpoint DELETE :
        main.mgr.cancel_track(tid)
        main.mgr.remove_job(tid)
        # Laisse le handler CancelledError s'exécuter.
        await asyncio.sleep(0.3)
        # Attendu (sain) : le job a disparu, aucune réécriture.
        assert tid not in main.mgr.jobs
        assert not (main.DATA_DIR / tid / "status.json").exists()

    asyncio.run(_run())
    _reset()
