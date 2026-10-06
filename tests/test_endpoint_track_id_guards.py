"""Tests des gardes ``_safe_track_id`` sur les endpoints (mp3 & bibliothèque).

Objectif : aucune route ne doit infliger un accès disque avec un identifiant
capable de sortir de ``DATA_DIR`` (``..``, ``.``, chemin absolu, séparateurs).
Chaque route doit répondre 404 avant tout accès.
"""
import json

import main
from tests.helpers import run_app


def _post_form(client, path, data):
    return client.post(path, data=data)


# --------------------------------------------------------------------------- #
# track_detail (GET /api/tracks/{id})
# --------------------------------------------------------------------------- #
def test_track_detail_rejects_traversal():
    async def _c(client):
        # Identifiants mono-segment malveillants : ils atteignent la route
        # `{track_id}` et doivent être rejetés par `_safe_track_id`. Les
        # chemins multi-segments (`../etc`, `a/b`) ne matchent pas la route
        # et renvoient aussi 404.
        for bad in ("../etc", "..", "/etc/passwd", "a/b",
                    ".hidden", "...", "..abc", "a\\b"):
            r = await client.get(f"/api/tracks/{bad}")
            assert r.status_code == 404, bad

    run_app(main.app, _c)


def test_track_detail_rejects_missing_valid_id():
    async def _c(client):
        r = await client.get("/api/tracks/valide_inconnu_20260101-120000")
        assert r.status_code == 404

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# status (GET /api/status/{id})
# --------------------------------------------------------------------------- #
def test_status_rejects_traversal():
    async def _c(client):
        for bad in ("../etc", "..", "...", "a\\b", ".hidden"):
            r = await client.get(f"/api/status/{bad}")
            assert r.status_code == 404, bad

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# bar-offset / structure-start (POST avec formulaire)
# --------------------------------------------------------------------------- #
def test_bar_offset_rejects_traversal():
    async def _c(client):
        r = await client.post("/api/tracks/../etc/bar-offset", data={"offset": "2"})
        assert r.status_code in (404, 405)

    run_app(main.app, _c)


def test_structure_start_rejects_traversal():
    async def _c(client):
        r = await client.post("/api/tracks/..%2Fetc/structure-start", data={"measure": "4"})
        assert r.status_code in (404, 405)

    run_app(main.app, _c)


def test_bar_offset_valid_track_but_missing_returns_404():
    async def _c(client):
        r = await client.post("/api/tracks/inconnu_12345678-120000/bar-offset",
                              data={"offset": "2"})
        assert r.status_code == 404

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# update_structure (POST JSON)
# --------------------------------------------------------------------------- #
def test_update_structure_rejects_traversal():
    async def _c(client):
        r = await client.post("/api/tracks/../etc/structure", json={"sections": {"1": "Intro"}})
        assert r.status_code in (404, 405)

    run_app(main.app, _c)
