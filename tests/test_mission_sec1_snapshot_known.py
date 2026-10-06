"""Tests MISSION-SEC-1 : ``JobManager.snapshot_known`` (garde « connu » atomique).

Objet du correctif :
  * le contrôle de connaissance ET la lecture de l'état sont réalisés sous un
    unique verrou (``self._lock``) : plus de lecture de ``mgr.jobs`` hors verrou ;
  * un identifiant inconnu (ni ``jobs``, ni ``status.json``, ni ``metadata.json``)
    renvoie ``None``, jamais l'état fantôme « queued » ;
  * quand ``status.json`` est absent, l'état est dérivé depuis ``metadata.json``
    (et non inventé en dur).

Ce fichier pilote ``main.mgr`` directement : aucun serveur, aucun réseau.
"""
import asyncio
import json

import main


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _reset():
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()
    main.mgr._cancelled.clear()


def _mk_status_json(track_id, status="processing", progress=42):
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({
        "id": track_id, "status": status, "progress": progress, "logs": ["étape"],
    }), encoding="utf-8")


def _mk_metadata_json(track_id, status="ready"):
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": track_id, "title": "Titre", "artist": "Artiste",
        "created_at": "2026-10-06T00:00:00Z",
    }
    if status is not None:
        payload["status"] = status
    (d / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Connaissance : renvoie None pour un inconnu (jamais le repli « queued »)
# --------------------------------------------------------------------------- #
def test_snapshot_known_unknown_returns_none():
    _reset()
    assert main.mgr.snapshot_known("jamais_vu_1") is None
    assert main.mgr.snapshot_known("jamais_vu_2") is None
    _reset()


def test_snapshot_known_unknown_does_not_resurrect_in_memory():
    """La réponse ``None`` ne doit ré-injecter aucune entrée fantôme dans ``jobs``."""
    _reset()
    tid = "no_ghost_1"
    main.mgr.snapshot_known(tid)
    assert tid not in main.mgr.jobs
    _reset()


# --------------------------------------------------------------------------- #
# 2. Connaissance : état en mémoire (jobs) prioritaire
# --------------------------------------------------------------------------- #
def test_snapshot_known_in_memory_job_returns_state():
    _reset()
    tid = "mem_ok_1"
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 44,
                          "logs": ["en cours"]}
    snap = main.mgr.snapshot_known(tid)
    assert snap is not None
    assert snap["status"] == "processing"
    assert snap["progress"] == 44
    _reset()


# --------------------------------------------------------------------------- #
# 3. Connaissance : status.json sur disque (hors jobs = après redémarrage)
# --------------------------------------------------------------------------- #
def test_snapshot_known_status_json_only_returns_state():
    _reset()
    tid = "disk_status_1"
    _mk_status_json(tid, status="processing", progress=55)
    snap = main.mgr.snapshot_known(tid)
    assert snap is not None
    assert snap["status"] == "processing"
    assert snap["progress"] == 55
    _reset()


# --------------------------------------------------------------------------- #
# 4. Dérivation depuis metadata.json quand status.json est absent
# --------------------------------------------------------------------------- #
def test_snapshot_known_metadata_only_derives_ready_progress_100():
    _reset()
    tid = "meta_ready_1"
    _mk_metadata_json(tid, status="ready")
    snap = main.mgr.snapshot_known(tid)
    assert snap is not None
    assert snap["status"] == "ready"
    assert snap["progress"] == 100
    assert snap["logs"] == []
    _reset()


def test_snapshot_known_metadata_only_derives_error_progress_0():
    _reset()
    tid = "meta_error_1"
    _mk_metadata_json(tid, status="error")
    snap = main.mgr.snapshot_known(tid)
    assert snap is not None
    assert snap["status"] == "error"
    assert snap["progress"] == 0
    _reset()


def test_snapshot_known_metadata_without_status_derives_unknown():
    """Un ``metadata.json`` sans clé ``status`` (relance partielle) est capté
    comme « connu », mais l'état dérivé ne prétend pas être « queued » : il
    signale l'absence d'information par ``status == 'unknown'``."""
    _reset()
    tid = "meta_nostatus_1"
    _mk_metadata_json(tid, status=None)
    snap = main.mgr.snapshot_known(tid)
    assert snap is not None
    assert snap["status"] == "unknown"
    assert snap["progress"] == 0
    _reset()


def test_snapshot_known_status_json_takes_priority_over_metadata():
    """Quand les deux fichiers existent, ``status.json`` (plus récent/volatile)
    prime sur le repli ``metadata.json``."""
    _reset()
    tid = "prio_1"
    _mk_status_json(tid, status="processing", progress=30)
    _mk_metadata_json(tid, status="ready")
    snap = main.mgr.snapshot_known(tid)
    assert snap["status"] == "processing"
    assert snap["progress"] == 30
    _reset()


# --------------------------------------------------------------------------- #
# 5. Après suppression : jamais l'état fantôme « queued »
# --------------------------------------------------------------------------- #
def test_snapshot_known_after_remove_job_returns_none():
    _reset()
    tid = "rm_1"
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 5, "logs": []}
    main.mgr.remove_job(tid)
    assert main.mgr.snapshot_known(tid) is None
    _reset()


def test_snapshot_known_remains_none_after_cancelled_flag():
    """Même si le thread ``to_thread`` (drapeau ``_cancelled``) est encore en cours,
    ``snapshot_known`` ne réinvente pas un état « queued »."""
    _reset()
    tid = "cancelled_none_1"
    main.mgr.cancel_track(tid)
    main.mgr.remove_job(tid)
    assert main.mgr.is_cancelled(tid) is True
    assert main.mgr.snapshot_known(tid) is None
    _reset()


# --------------------------------------------------------------------------- #
# 6. Atomicité : la lecture d'état se fait sous le verrou
# --------------------------------------------------------------------------- #
def test_snapshot_known_reads_state_under_lock(monkeypatch):
    """Vérifie que ``_load_job_locked`` (donc la lecture de ``jobs``) est appelé
    alors que ``self._lock`` est bien tenu (pas de lecture hors verrou)."""
    _reset()
    tid = "lock_probe_1"
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 40, "logs": []}

    orig = main.mgr._load_job_locked
    seen = {}

    def wrapper(track_id):
        seen["locked"] = main.mgr._lock.locked()
        return orig(track_id)

    monkeypatch.setattr(main.mgr, "_load_job_locked", wrapper)
    snap = main.mgr.snapshot_known(tid)
    assert seen.get("locked") is True
    assert snap["status"] == "processing"
    _reset()


def test_snapshot_reads_state_under_lock(monkeypatch):
    """Le ``snapshot`` (repli historique) tient lui aussi le verrou avant de lire."""
    _reset()
    tid = "lock_probe_2"
    main.mgr.jobs[tid] = {"id": tid, "status": "processing", "progress": 10, "logs": []}

    orig = main.mgr._load_job_locked
    seen = {}

    def wrapper(track_id):
        seen["locked"] = main.mgr._lock.locked()
        return orig(track_id)

    monkeypatch.setattr(main.mgr, "_load_job_locked", wrapper)
    snap = main.mgr.snapshot(tid)
    assert seen.get("locked") is True
    assert snap["progress"] == 10
    _reset()


# --------------------------------------------------------------------------- #
# 6 bis. Défaut connu (TOCTOU) : « queued » fantôme lors d'un DELETE concurrent
# --------------------------------------------------------------------------- #
def test_snapshot_known_no_phantom_queued_on_concurrent_delete(monkeypatch):
    """Régression MISSION-SEC-1 : un DELETE concurrent ne doit JAMAIS produire
    un état « queued » fantôme pour un morceau dont les artefacts ont disparu.

    Reproduction : entre le contrôle « connu » (status.json/metadata.json
    présents) et la lecture `_load_job_locked`, le disque est vidé (comme le
    ferait `shutil.rmtree` du DELETE). Attendu : ``None`` (404). Le correctif M1
    ajoute un contre-contrôle sous `_lock` après la lecture : si l'état lu est
    « queued » alors qu'aucune source de vérité ne subsiste, on renvoie ``None``.
    """
    _reset()
    tid = "touctou_sec1_1"
    _mk_metadata_json(tid, status="ready")

    orig = main.mgr._load_job_locked

    def _concurrent_rmtree(track_id):
        # Simule la suppression disque faite par `delete_library_track`
        # (shutil.rmtree), qui ne tient pas `_lock`.
        d = main.DATA_DIR / track_id
        for f in (d / "status.json", d / "metadata.json"):
            if f.exists():
                f.unlink()
        return orig(track_id)

    monkeypatch.setattr(main.mgr, "_load_job_locked", _concurrent_rmtree)
    assert main.mgr.snapshot_known(tid) is None
    _reset()


# --------------------------------------------------------------------------- #
# 7. Non-régression M1 : les « queued » légitimes ne doivent PAS être masqués
# --------------------------------------------------------------------------- #
def test_snapshot_known_keeps_legit_in_memory_queued():
    """Un morceau fraîchement lancé (``jobs`` contient un état « queued » réel)
    reste « queued » : le contre-contrôle M1 ne le convertit PAS en ``None``."""
    _reset()
    tid = "legit_mem_queued_1"
    main.mgr.jobs[tid] = {"id": tid, "status": "queued", "progress": 0,
                          "logs": ["Tâche mise en file."]}
    snap = main.mgr.snapshot_known(tid)
    assert snap is not None
    assert snap["status"] == "queued"
    assert snap["logs"] == ["Tâche mise en file."]
    _reset()


def test_snapshot_known_keeps_legit_disk_queued():
    """Un ``status.json`` présent sur disque avec ``status == 'queued'``
    (redémarrage avant traitement) reste « queued » : M1 ne le masque pas."""
    _reset()
    tid = "legit_disk_queued_1"
    _mk_status_json(tid, status="queued", progress=0)
    snap = main.mgr.snapshot_known(tid)
    assert snap is not None
    assert snap["status"] == "queued"
    assert snap["progress"] == 0
    _reset()


def test_snapshot_known_keeps_legit_queued_after_start(monkeypatch):
    """Après ``mgr.start()``, le polling immédiat (état « queued » en mémoire)
    est conservé : le contre-contrôle ne voit pas un état « fantôme »."""
    _reset()
    tid = "legit_after_start_1"
    monkeypatch.setattr(main.mgr, "_pipeline", lambda t, s: None)

    async def _run():
        main.mgr.loop = asyncio.get_running_loop()
        main.mgr.start(tid, {"type": "upload"})
        snap = main.mgr.snapshot_known(tid)
        assert snap is not None
        # `start` journalise immédiatement (log status="processing") : l'état
        # réel est donc « processing » (ou « queued » avant la 1re log) — surtout
        # PAS un état fantôme masqué par M1.
        assert snap["status"] in ("queued", "processing")
        await main.mgr.shutdown()

    asyncio.run(_run())
    _reset()


# --------------------------------------------------------------------------- #
# 8. No side-effect : snapshot_known n'écrit rien sur disque
# --------------------------------------------------------------------------- #
def test_snapshot_known_writes_nothing_on_disk():
    _reset()
    tid = "no_side_sec1_1"
    d = main.DATA_DIR / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"id": tid, "status": "ready",
                                               "progress": 100, "logs": []}),
                                   encoding="utf-8")
    before = {
        str(p.relative_to(main.DATA_DIR)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in main.DATA_DIR.rglob("*") if p.is_file()
    }
    main.mgr.snapshot_known(tid)
    after = {
        str(p.relative_to(main.DATA_DIR)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in main.DATA_DIR.rglob("*") if p.is_file()
    }
    assert after == before
    _reset()
