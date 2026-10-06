"""Tests d'intégration de ``POST /api/process`` (YouTube + upload).

Le pipeline lourd (demucs/whisper…) est neutralisé : ``mgr.start`` est mocké
et la résolution du titre YouTube aussi.
"""
import main
from tests.helpers import run_app


def _reset_mgr():
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    main.mgr.listeners.clear()


def test_process_no_input_returns_400(monkeypatch):
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)

    async def _c(client):
        r = await client.post("/api/process")
        assert r.status_code == 400

    run_app(main.app, _c)


def test_process_rejects_non_youtube_url(monkeypatch):
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)

    async def _c(client):
        r = await client.post("/api/process", data={"url": "http://evil.com/x"})
        assert r.status_code == 400
        assert "URL non autorisée" in r.text

    run_app(main.app, _c)


def test_process_rejects_localhost_url(monkeypatch):
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)

    async def _c(client):
        r = await client.post("/api/process", data={"url": "http://127.0.0.1/x"})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_process_accepts_youtube_url(monkeypatch):
    _reset_mgr()
    calls = []
    monkeypatch.setattr(main.mgr, "start", lambda tid, src: calls.append((tid, src)))
    monkeypatch.setattr(main, "fetch_youtube_title", lambda url: "Ma Chanson")

    async def _c(client):
        r = await client.post("/api/process", data={"url": "https://youtu.be/abc"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "queued"
        assert len(calls) == 1
        tid, src = calls[0]
        assert src["type"] == "youtube"
        assert src["url"] == "https://youtu.be/abc"
        assert tid.startswith("ma-chanson_")

    run_app(main.app, _c)


def test_process_rejects_unsupported_extension(monkeypatch):
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)

    async def _c(client):
        r = await client.post(
            "/api/process",
            files={"file": ("song.exe", b"ID3\x03\x00\x00", "application/octet-stream")},
        )
        assert r.status_code == 415

    run_app(main.app, _c)


def test_process_rejects_bad_magic_bytes(monkeypatch):
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)

    async def _c(client):
        r = await client.post(
            "/api/process",
            files={"file": ("song.mp3", b"<html>not audio</html>", "audio/mpeg")},
        )
        assert r.status_code == 415

    run_app(main.app, _c)


def test_process_accepts_valid_upload(monkeypatch):
    _reset_mgr()
    calls = []
    monkeypatch.setattr(main.mgr, "start", lambda tid, src: calls.append((tid, src)))

    async def _c(client):
        r = await client.post(
            "/api/process",
            files={"file": ("song.mp3", b"ID3\x04\x00\x00\x00some", "audio/mpeg")},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert len(calls) == 1
        _, src = calls[0]
        assert src["type"] == "upload"
        assert src["filename"] == "song.mp3"
        # Le fichier a bien été écrit sur disque.
        assert src["raw_path"].exists()

    run_app(main.app, _c)


def test_process_rejects_oversized_upload(monkeypatch):
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)
    monkeypatch.setattr(main, "MAX_UPLOAD", 8)  # 8 octets max pour ce test

    async def _c(client):
        payload = b"ID3\x04" + b"x" * 100
        r = await client.post(
            "/api/process",
            files={"file": ("big.mp3", payload, "audio/mpeg")},
        )
        assert r.status_code == 413
        assert "500 Mo" in r.text

    run_app(main.app, _c)


def test_process_rejects_oversized_upload_without_leftover_file(monkeypatch):
    """Après un rejet 413, aucun fichier partiel ne doit rester sur disque."""
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)
    monkeypatch.setattr(main, "MAX_UPLOAD", 8)
    before = set(main.DATA_DIR.rglob("*")) if main.DATA_DIR.exists() else set()

    async def _c(client):
        r = await client.post(
            "/api/process",
            files={"file": ("big.mp3", b"ID3\x04" + b"y" * 300, "audio/mpeg")},
        )
        assert r.status_code == 413
        # Le fichier partiel a été supprimé ; le dossier `source/` ne doit pas
        # contenir le fichier rejeté.
        leftovers = [p for p in main.DATA_DIR.rglob("*") if p.is_file() and p not in before]
        assert not [p for p in leftovers if p.suffix.lower() == ".mp3"], leftovers

    run_app(main.app, _c)


def test_process_rejects_bad_magic_without_leftover_file(monkeypatch):
    """Après un rejet 415 (magic bytes), aucun NOUVEAU fichier ne reste."""
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)
    before = {p for p in main.DATA_DIR.rglob("*.mp3")} if main.DATA_DIR.exists() else set()

    async def _c(client):
        r = await client.post(
            "/api/process",
            files={"file": ("fake.mp3", b"<html><script>alert(1)</script>", "audio/mpeg")},
        )
        assert r.status_code == 415
        after = {p for p in main.DATA_DIR.rglob("*.mp3")}
        assert not (after - before), after - before

    run_app(main.app, _c)


def test_process_rejects_extension_before_creating_dir(monkeypatch):
    """Le contrôle d'extension précède la création du dossier source."""
    _reset_mgr()
    monkeypatch.setattr(main.mgr, "start", lambda *a, **k: None)

    async def _c(client):
        before_dirs = set(main.DATA_DIR.iterdir()) if main.DATA_DIR.exists() else set()
        r = await client.post(
            "/api/process",
            files={"file": ("song.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert r.status_code == 415
        after_dirs = set(main.DATA_DIR.iterdir())
        # Aucun dossier de morceau ne doit avoir été créé par ce POST.
        assert not (after_dirs - before_dirs)

    run_app(main.app, _c)
