"""Tests de l'endpoint ``POST /api/tracks/{id}/structure`` (sanitisation XSS/métadonnées)."""
import json

import main
from tests.helpers import run_app


def _make_track(track_id="ma_chanson_20250101-120000"):
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps({"id": track_id, "title": "Ma Chanson"}))
    return track_id


def _post_structure(client, track_id, payload):
    return client.post(f"/api/tracks/{track_id}/structure", json=payload)


def test_structure_valid_sections_and_line_breaks():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {
            "sections": {"1": "Intro", "5": "Couplet"},
            "line_breaks": [2, 4],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["sections"] == {"1": "Intro", "5": "Couplet"}
        assert body["line_breaks"] == [2, 4]
        # Persisté sur disque.
        meta = json.loads((main.DATA_DIR / track_id / "metadata.json").read_text("utf-8"))
        assert meta["sections"] == {"1": "Intro", "5": "Couplet"}

    run_app(main.app, _c)


def test_structure_rejects_negative_measure():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"sections": {"-1": "Intro"}})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_structure_rejects_non_integer_measure():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"sections": {"abc": "Intro"}})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_structure_rejects_empty_label():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"sections": {"1": "   "}})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_structure_rejects_too_long_label():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"sections": {"1": "A" * 81}})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_structure_rejects_control_characters():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"sections": {"1": "Intro\nBad"}})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_structure_stores_html_like_label_for_client_side_escaping():
    """Le serveur ne bloque pas `<script>` (l'échappement est côté client via `esc`)."""
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"sections": {"1": "<script>alert(1)</script>"}})
        assert r.status_code == 200
        # Stocké tel quel ; la protection XSS est assurée par `esc()` dans static/app.js.
        meta = json.loads((main.DATA_DIR / track_id / "metadata.json").read_text("utf-8"))
        assert meta["sections"]["1"] == "<script>alert(1)</script>"

    run_app(main.app, _c)


def test_structure_rejects_negative_line_break():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"line_breaks": [1, -1]})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_structure_rejects_non_integer_line_break():
    track_id = _make_track()

    async def _c(client):
        r = await _post_structure(client, track_id, {"line_breaks": ["a"]})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_structure_unknown_track_returns_404():
    async def _c(client):
        r = await _post_structure(client, "inconnue_123", {"sections": {"1": "Intro"}})
        assert r.status_code == 404

    run_app(main.app, _c)
