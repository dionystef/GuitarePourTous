"""Tests de l'endpoint WebSocket ``/api/ws/{track_id}`` (garde-fous de sécurité).

On pilote ``ws_status`` directement avec un double de WebSocket ; aucun
transport réseau réel n'est utilisé.
"""
import asyncio
import json

import main
from tests.helpers import FakeWebSocket, run_app


def _run(ws, track_id):
    async def _invoke():
        try:
            await main.ws_status(ws, track_id)
        except Exception:
            pass

    asyncio.run(_invoke())


def _reset_mgr():
    """Remet le JobManager dans un état vierge (mémoire + annulation)."""
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()
    main.mgr._cancelled.clear()


def _ensure_known(track_id, status="queued", progress=0):
    """Matérialise un morceau « connu » via ``status.json`` pour les tests qui
    doivent s'abonner au flux d'un identifiant valide. Depuis MISSION-SEC-1,
    ws_status ferme la connexion pour un identifiant inconnu/supprimé."""
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({
        "id": track_id, "status": status, "progress": progress, "logs": [],
    }), encoding="utf-8")


def _ensure_metadata_known(track_id, status="ready"):
    """Matérialise un morceau « connu » par ``metadata.json`` seul (pas de
    ``status.json``) : l'état dérivé sert à vérifier la cohérence HTTP/WS."""
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps({
        "id": track_id, "status": status, "title": "Titre", "artist": "Artiste",
        "created_at": "2026-10-06T00:00:00Z",
    }), encoding="utf-8")


def test_ws_rejects_invalid_track_id_before_accept():
    ws = FakeWebSocket([])
    _run(ws, "../etc")
    assert ws.accepted is False
    assert ws.closed == 1008


def test_ws_accepts_valid_track_and_pings():
    ws = FakeWebSocket(["ping"])
    _ensure_known("good_track_1")
    _run(ws, "good_track_1")
    assert ws.accepted is True
    assert ws.closed is None
    # Statut initial + pong.
    assert ws.sent[0]["type"] == "status"
    assert ws.sent[1] == {"type": "pong"}


def test_ws_closes_on_oversized_frame():
    # Le garde-fou applicatif limite à 4096 caractères (au-delà → 1009).
    ws = FakeWebSocket(["x" * 5000])
    _ensure_known("good_track_2")
    _run(ws, "good_track_2")
    assert ws.accepted is True
    assert ws.closed == 1009


def test_ws_ignores_non_ping_message_and_does_not_close():
    """Un message qui n'est pas `ping` est ignoré sans fermer la connexion."""
    ws = FakeWebSocket(["hello", "world"])
    _ensure_known("good_track_3")
    _run(ws, "good_track_3")
    assert ws.accepted is True
    assert ws.closed is None
    # Seul le statut initial est envoyé (aucun pong pour "hello").
    assert ws.sent == [{"type": "status", "status": {"id": "good_track_3",
                                                     "status": "queued",
                                                     "progress": 0,
                                                     "logs": []}}]


def test_ws_removes_listener_on_disconnect():
    """Après déconnexion, le listener doit être retiré des abonnés."""
    main.mgr.listeners.clear()
    ws = FakeWebSocket([])  # receive_text lève immédiatement WebSocketDisconnect
    _ensure_known("good_track_4")
    _run(ws, "good_track_4")
    assert ws.accepted is True
    # Le listener a bien été ajouté puis retiré (set vide / clé absente).
    assert not main.mgr.listeners.get("good_track_4")
    main.mgr.listeners.clear()


def test_ws_rejects_dot_segment_id():
    """Un identifiant '.' / '..' (segment seul) est rejeté avant accept."""
    for bad in (".", ".."):
        ws = FakeWebSocket([])
        _run(ws, bad)
        assert ws.accepted is False
        assert ws.closed == 1008


# --------------------------------------------------------------------------- #
# MISSION-SEC-1 : cohérence HTTP/WS + comportement après DELETE
# --------------------------------------------------------------------------- #
def test_ws_closes_on_unknown_valid_id_before_accept():
    """Un identifiant bien formé mais INCONNU (aucun artifact sur disque, hors
    ``jobs``) est fermé avec 1008, exactement comme HTTP renvoie 404."""
    _reset_mgr()
    ws = FakeWebSocket([])
    _run(ws, "inconnu_bien_forme_1")
    assert ws.accepted is False
    assert ws.closed == 1008
    _reset_mgr()


def test_http_and_ws_agree_on_unknown_id():
    """Divergence interdite : pour un id inconnu, HTTP renvoie 404 ET WS ferme
    en 1008 (aucun état « queued » fantôme par l'un des deux canaux)."""
    _reset_mgr()

    async def _c(client):
        r = await client.get("/api/status/inconnu_agreement_1")
        assert r.status_code == 404, r.text

    run_app(main.app, _c)
    ws = FakeWebSocket([])
    _run(ws, "inconnu_agreement_1")
    assert ws.accepted is False
    assert ws.closed == 1008
    _reset_mgr()


def test_http_and_ws_agree_on_metadata_only_known_id():
    """Pour un morceau connu uniquement par ``metadata.json``, HTTP et WS
    renvoient le MÊME état dérivé (status 'ready', progress 100) : pas de
    divergence, pas d'invention d'un « queued » côté socket."""
    _reset_mgr()
    tid = "meta_agree_1"
    _ensure_metadata_known(tid, status="ready")

    async def _c(client):
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ready"
        assert body["progress"] == 100

    run_app(main.app, _c)

    ws = FakeWebSocket([])
    _run(ws, tid)
    assert ws.accepted is True
    assert ws.sent[0]["type"] == "status"
    initial = ws.sent[0]["status"]
    assert initial["status"] == "ready"
    assert initial["progress"] == 100
    _reset_mgr()


def test_ws_initial_status_matches_http_status():
    """Cohérence : l'état initial poussé par la socket est identique à celui
    exposé par ``GET /api/status`` pour le même morceau."""
    _reset_mgr()
    tid = "consistent_1"
    _ensure_known(tid, status="processing", progress=33)

    http_body = {}

    async def _c(client):
        r = await client.get(f"/api/status/{tid}")
        assert r.status_code == 200, r.text
        http_body.update(r.json())

    run_app(main.app, _c)
    ws = FakeWebSocket([])
    _run(ws, tid)
    assert ws.accepted is True
    assert ws.sent[0]["status"] == http_body
    _reset_mgr()


def test_ws_snapshot_captured_once(monkeypatch):
    """M2 : ``snapshot_known`` n'est appelé qu'UNE fois (capture à la garde).
    Un DELETE concurrent pendant le handshake ne peut donc pas injecter un
    ``"status": null`` dans la trame initiale (régression « status » null)."""
    _reset_mgr()
    tid = "ws_capture_once_1"
    _ensure_known(tid, status="processing", progress=50)

    calls = []
    real = main.mgr.snapshot_known

    def counting(track_id):
        calls.append(track_id)
        # Simule un DELETE pendant l'accept : un 2e appel renverrait None.
        if len(calls) == 2:
            return None
        return real(track_id)

    monkeypatch.setattr(main.mgr, "snapshot_known", counting)

    ws = FakeWebSocket([])
    _run(ws, tid)
    assert ws.accepted is True
    assert len(calls) == 1, f"snapshot_known appelé {len(calls)} fois (attendu 1)"
    assert ws.sent[0]["type"] == "status"
    assert ws.sent[0]["status"] is not None
    assert ws.sent[0]["status"]["status"] == "processing"
    assert ws.sent[0]["status"]["progress"] == 50
    _reset_mgr()


def test_ws_closes_1008_after_delete(monkeypatch):
    """Régression MISSION-SEC-1 : après un ``DELETE`` (cancel + rmtree), même si
    le pipeline est encore en cours, la socket est fermée en 1008 — jamais
    d'état « queued »/« processing » résiduel envoyé. Le thread ``to_thread``
    non interrompable tente de re-journaliser après annulation : ``log`` doit
    l'ignorer pour ne pas recréer d'artefact."""
    _reset_mgr()
    tid = "ws_after_delete_1"
    # Le morceau existe sur disque (nécessaire pour que DELETE renvoie 200).
    _ensure_metadata_known(tid, status="ready")

    def _slow_pipeline(track_id, source):
        import time
        time.sleep(0.05)
        # Le thread poursuit puis tente de journaliser après l'annulation.
        main.mgr.log(track_id, "étape post-annulation", 60)
        time.sleep(0.05)

    monkeypatch.setattr(main.mgr, "_pipeline", _slow_pipeline)

    async def _c(client):
        main.mgr.start(tid, {"type": "upload"})
        await asyncio.sleep(0.05)  # laisse le thread du pipeline démarrer
        r = await client.delete(f"/api/tracks/{tid}")
        assert r.status_code == 200, r.text
        # Après suppression : l'état HTTP est bien 404 (pas de fantôme).
        r2 = await client.get(f"/api/status/{tid}")
        assert r2.status_code == 404, r2.text

    run_app(main.app, _c)
    ws = FakeWebSocket([])
    _run(ws, tid)
    assert ws.accepted is False
    assert ws.closed == 1008
    _reset_mgr()
