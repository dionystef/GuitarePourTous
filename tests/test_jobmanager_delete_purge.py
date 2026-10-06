"""Tests MISSION-SEC (phase 2) du ``JobManager``.

Cible les correctifs de non-résurrection après ``DELETE`` :
  * purge mémoire + disque (``remove_job``) ;
  * drapeau ``_cancelled`` / ``is_cancelled`` (le thread ``to_thread`` non
    interrompable doit s'auto-supprimer et ne pas recréer les artefacts) ;
  * garde ``is_cancelled`` avant l'écriture de ``metadata.json`` dans
    ``_pipeline`` (et dans son branche d'erreur) ;
  * ``shutdown()`` (tâches actives, sans tâche, double appel) ;
  * absence de deadlock du verrou ``_lock`` (``start`` → ``log`` → ``snapshot``).

Aucune modification du code métier : on pilote ``main.mgr`` directement ou on
remplace ``_pipeline`` / les services lourds par des doublures légères.
"""
import asyncio
import json
import time

import pytest

import main


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Méthode ``_pipeline`` réelle (méthode liée à l'instance) : utilisée pour
# restaurer le pipeline après les tests qui le remplacent par un stub.
_REAL_PIPELINE = main.JobManager._pipeline.__get__(main.mgr)


def _bind_loop():
    loop = asyncio.get_running_loop()
    main.mgr.loop = loop
    return loop


def _use_real_pipeline():
    """Force l'appel du vrai ``_pipeline`` (et non d'un stub résiduel)."""
    main.mgr._pipeline = _REAL_PIPELINE


def _reset():
    """Remet ``mgr`` dans un état vierge (y compris ``_cancelled``)."""
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()
    main.mgr._cancelled.clear()
    main.mgr.loop = None
    main.mgr._pipeline = _REAL_PIPELINE


def _sleep_pipeline(track_id, source, delay=0.2):
    time.sleep(delay)


def _late_logger_pipeline(track_id, source):
    """Simule un thread ``to_thread`` qui tente d'écrire APRÈS l'annulation."""
    time.sleep(0.05)
    main.mgr.log(track_id, "étape tardive 1", 50)
    time.sleep(0.05)
    main.mgr.log(track_id, "étape tardive 2", 60)


def _patch_heavy_services(monkeypatch):
    """Remplace les services lourds du pipeline par des fakes synchrones."""
    import types

    def _ingest_youtube(url, track_dir):
        return "/x/wav", {
            "title": "Titre test", "duration": 120, "artist": "Artiste",
            "source_url": url, "thumbnail": "", "created_at": "now",
        }

    def _separate_stems(wav, track_dir, device):
        return ["vocals", "drums"], None

    def _analyze_harmony(wav):
        return {"bpm": 120, "bars": [1, 2], "beats_per_bar": 4,
                "time_signature": "4/4"}

    def _resolve_lyrics(*args, **kwargs):
        return ["ligne de parole"], "none"

    monkeypatch.setattr(main, "ingest_youtube", _ingest_youtube)
    monkeypatch.setattr(main, "separate_stems", _separate_stems)
    monkeypatch.setattr(main, "analyze_harmony", _analyze_harmony)
    monkeypatch.setattr(main, "resolve_lyrics", _resolve_lyrics)


# --------------------------------------------------------------------------- #
# remove_job : mémoire + disque + robustesse
# --------------------------------------------------------------------------- #
def test_remove_job_purges_memory_and_status_json():
    _reset()
    tid = "remove_track_1"
    d = main.DATA_DIR / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"status": "processing"}))

    async def _run():
        _bind_loop()
        main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 1,
                              "logs": []}
        main.mgr.remove_job(tid)
        assert tid not in main.mgr.jobs
        assert not (main.DATA_DIR / tid / "status.json").exists()

    asyncio.run(_run())
    _reset()


def test_remove_job_missing_track_is_noop():
    _reset()
    main.mgr.remove_job("ne_veut_pas_exister_1")  # ne doit pas lever d'exception
    assert "ne_veut_pas_exister_1" not in main.mgr.jobs


def test_remove_job_keeps_cancelled_flag():
    """Le flag d'annulation doit SURVIVRE à remove_job : le thread ``to_thread``
    encore en cours reste « annulé » et ne recrée pas d'artefact."""
    _reset()
    tid = "cancelled_keep_1"
    main.mgr.cancel_track(tid)
    main.mgr.remove_job(tid)
    assert main.mgr.is_cancelled(tid) is True


# --------------------------------------------------------------------------- #
# is_cancelled : défaut, après annulation, reset via start
# --------------------------------------------------------------------------- #
def test_is_cancelled_default_false():
    _reset()
    assert main.mgr.is_cancelled("no_annulation_1") is False


def test_is_cancelled_true_after_cancel():
    _reset()
    tid = "cancel_flag_1"
    main.mgr.cancel_track(tid)
    assert main.mgr.is_cancelled(tid) is True


def test_start_resets_cancelled_flag():
    _reset()
    tid = "reset_flag_1"

    async def _run():
        _bind_loop()
        main.mgr.cancel_track(tid)
        assert main.mgr.is_cancelled(tid) is True
        main.mgr.start(tid, {"type": "upload"})
        # `start` purge le drapeau résiduel : le nouvel essai repart proprement.
        assert main.mgr.is_cancelled(tid) is False

    asyncio.run(_run())
    _reset()


# --------------------------------------------------------------------------- #
# Non-résurrection après DELETE (mémoire + statut + handler CancelledError)
# --------------------------------------------------------------------------- #
def test_after_delete_track_not_resurrected_and_status_json_gone():
    """Régression : DELETE (cancel+remove) doit rendre le job introuvable.

    Le handler ``CancelledError`` de ``_job`` purge (via ``remove_job``) au lieu
    de ré-écrire l'état ; le thread ``to_thread`` non interrompable ne doit pas
    recréer ``status.json``.
    """
    import json as _json
    _reset()
    tid = "delete_resurrect_1"
    d = main.DATA_DIR / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(_json.dumps({"status": "processing"}))
    main.mgr._pipeline = _sleep_pipeline

    async def _run():
        _bind_loop()
        main.mgr.start(tid, {"type": "upload"})
        await asyncio.sleep(0.05)
        # Ordre exact de l'endpoint DELETE :
        main.mgr.cancel_track(tid)
        main.mgr.remove_job(tid)
        await asyncio.sleep(0.3)  # laisse le handler CancelledError s'exécuter
        assert tid not in main.mgr.jobs
        assert tid not in main.mgr.tasks
        assert not (main.DATA_DIR / tid / "status.json").exists()

    asyncio.run(_run())
    _reset()


def test_cancelled_thread_does_not_recreate_status_json():
    """Un ``to_thread`` qui journalise APRÈS l'annulation est supprimé par le
    drapeau ``is_cancelled`` : ``status.json`` ne doit pas réapparaître."""
    _reset()
    tid = "late_write_1"
    main.mgr._pipeline = _late_logger_pipeline

    async def _run():
        _bind_loop()
        main.mgr.start(tid, {"type": "upload"})
        await asyncio.sleep(0.02)  # le thread a démarré et dort
        main.mgr.cancel_track(tid)
        main.mgr.remove_job(tid)
        await asyncio.sleep(0.25)  # le thread poursuit puis tente de logger
        assert not (main.DATA_DIR / tid / "status.json").exists()
        assert tid not in main.mgr.jobs

    asyncio.run(_run())
    _reset()


# --------------------------------------------------------------------------- #
# Garde is_cancelled avant l'écriture de metadata.json (pipeline réel)
# --------------------------------------------------------------------------- #
def test_cancelled_pipeline_does_not_write_metadata(monkeypatch):
    _reset()
    _use_real_pipeline()
    _patch_heavy_services(monkeypatch)
    tid = "cancel_pipeline_1"
    main.mgr._cancelled.add(tid)
    main.mgr._pipeline(tid, {"type": "youtube", "url": "https://youtube.com/watch?v=1"})
    assert not (main.DATA_DIR / tid / "metadata.json").exists()
    assert not (main.DATA_DIR / tid / "status.json").exists()
    _reset()


def test_non_cancelled_pipeline_writes_metadata(monkeypatch):
    """Régression : sans annulation, la finalisation produit bien ses artefacts."""
    _reset()
    _use_real_pipeline()
    _patch_heavy_services(monkeypatch)
    tid = "ok_pipeline_1"
    main.mgr._pipeline(tid, {"type": "youtube", "url": "https://youtube.com/watch?v=2"})
    meta_path = main.DATA_DIR / tid / "metadata.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text("utf-8"))
    assert meta["status"] == "ready"
    assert "files" in meta
    _reset()


def test_cancelled_error_branch_does_not_write_error_metadata(monkeypatch):
    """Même en cas d'exception, un morceau annulé ne doit pas publier de
    ``metadata.json`` « error » (garde de la branche ``except``)."""
    _reset()
    _use_real_pipeline()
    _patch_heavy_services(monkeypatch)

    def _boom(wav):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(main, "analyze_harmony", _boom)
    tid = "cancel_error_1"
    main.mgr._cancelled.add(tid)
    main.mgr._pipeline(tid, {"type": "youtube", "url": "https://youtube.com/watch?v=3"})
    assert not (main.DATA_DIR / tid / "metadata.json").exists()
    _reset()


def test_non_cancelled_error_branch_writes_error_metadata(monkeypatch):
    """Régression : sans annulation, une erreur doit produire un
    ``metadata.json`` « error » documenté (mais sans fuite de message)."""
    _reset()
    _use_real_pipeline()
    _patch_heavy_services(monkeypatch)

    def _boom(wav):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(main, "analyze_harmony", _boom)
    tid = "error_meta_1"
    main.mgr._pipeline(tid, {"type": "youtube", "url": "https://youtube.com/watch?v=4"})
    meta_path = main.DATA_DIR / tid / "metadata.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text("utf-8"))
    assert meta["status"] == "error"
    # Le message générique ne doit pas exposer la source/chemin absolu.
    assert "panne simulée" not in meta.get("error", "")
    _reset()


# --------------------------------------------------------------------------- #
# Verrou non réentrant : pas de deadlock start → log → snapshot
# --------------------------------------------------------------------------- #
def test_no_deadlock_nested_start_log_snapshot():
    _reset()
    tid = "deadlock_free_1"

    async def _run():
        _bind_loop()
        main.mgr.start(tid, {"type": "upload"})
        # `start` appelle déjà `self.log` en interne ; on enchaîne d'autres
        # accès verrouillés pour vérifier l'absence de deadlock.
        main.mgr.log(tid, "message de test", 33)
        main.mgr.log(tid, "second message", 66)
        snap = main.mgr.snapshot(tid)
        assert snap["status"] == "processing"
        assert snap["progress"] == 66
        assert snap["logs"][-1] == "second message"

    # timeout : si un deadlock se produit, asyncio.run ne se termine jamais.
    asyncio.run(asyncio.wait_for(_run(), timeout=2))


def test_snapshot_after_remove_returns_default_without_resurrecting():
    """Après ``remove_job``, ``snapshot`` renvoie l'état par défaut mais ne
    ré-injecte PLUS l'entrée dans ``jobs`` (cohérence de ``_status_obj``)."""
    _reset()
    tid = "snapshot_after_remove_1"

    async def _run():
        _bind_loop()
        main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 5,
                              "logs": []}
        main.mgr.remove_job(tid)
        snap = main.mgr.snapshot(tid)
        assert snap["status"] == "queued"   # repli disque/progress
        assert tid not in main.mgr.jobs      # pas de résurrection

    asyncio.run(_run())
    _reset()


# --------------------------------------------------------------------------- #
# shutdown : actif, sans tâche, double appel
# --------------------------------------------------------------------------- #
def test_shutdown_no_tasks_is_noop():
    _reset()

    async def _run():
        _bind_loop()
        await main.mgr.shutdown()  # aucun tâche : ne doit pas lever

    asyncio.run(_run())
    _reset()


def test_shutdown_cancels_active_task_and_clears_tasks():
    _reset()
    main.mgr._pipeline = _sleep_pipeline

    async def _run():
        _bind_loop()
        main.mgr.start("shutdown_active_1", {"type": "upload"})
        await asyncio.sleep(0.05)
        assert "shutdown_active_1" in main.mgr.tasks
        await main.mgr.shutdown()
        assert "shutdown_active_1" not in main.mgr.tasks

    asyncio.run(_run())
    _reset()


def test_shutdown_double_call_is_safe():
    _reset()
    main.mgr._pipeline = _sleep_pipeline

    async def _run():
        _bind_loop()
        main.mgr.start("shutdown_double_1", {"type": "upload"})
        await asyncio.sleep(0.05)
        await main.mgr.shutdown()
        await main.mgr.shutdown()  # second appel : plus rien à annuler
        assert "shutdown_double_1" not in main.mgr.tasks

    asyncio.run(_run())
    _reset()
