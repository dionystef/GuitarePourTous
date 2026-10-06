"""Tests de l'endpoint ``GET /data/{file_path:path}`` (serve_data_file).

Couvre : accès autorisé dans ``DATA_DIR``, rejet via ``../`` et via les
répertoires frères, fichier inexistant, et requêtes HTTP Range.
"""
import asyncio
from pathlib import Path

import main
from fastapi import HTTPException
from tests.helpers import run_app


class _FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _FakeRequest:
    """Mini-requête Starlette : seul ``headers`` est utilisé par l'endpoint."""

    def __init__(self, range_header=None):
        self.headers = _FakeHeaders()
        if range_header:
            self.headers["range"] = range_header


def _make_track_file(relative: str, content: bytes) -> Path:
    target = main.DATA_DIR / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _make_outer_secret() -> Path:
    """Fichier situé DANS le parent de DATA_DIR (donc en dehors de celui-ci)."""
    secret = main.DATA_DIR.parent / "guitarlab_secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("SECRET")
    return secret


def test_serve_file_inside_data_dir():
    _make_track_file("t1/source/song.mp3", b"ABCDEFGHIJ")

    async def _c(client):
        r = await client.get("/data/t1/source/song.mp3")
        assert r.status_code == 200
        assert r.content == b"ABCDEFGHIJ"
        assert r.headers.get("content-type")  # media_type résolu

    run_app(main.app, _c)


def test_serve_file_rejects_parent_traversal(tmp_path):
    # On crée un fichier en dehors de DATA_DIR et on tente d'y accéder via ../.
    secret = _make_outer_secret()
    rel = f"../{secret.name}"

    async def _c(client):
        r = await client.get(f"/data/{rel}")
        assert r.status_code == 404

    run_app(main.app, _c)


def test_serve_file_rejects_nonexistent():
    async def _c(client):
        r = await client.get("/data/does_not_exist.mp3")
        assert r.status_code == 404

    run_app(main.app, _c)


def test_serve_file_range_request():
    _make_track_file("t1/source/song.mp3", b"ABCDEFGHIJ")

    async def _c(client):
        r = await client.get("/data/t1/source/song.mp3", headers={"Range": "bytes=2-5"})
        assert r.status_code == 206
        assert r.headers.get("content-range") == "bytes 2-5/10"
        assert r.content == b"CDEF"

    run_app(main.app, _c)


def test_serve_file_handler_rejects_traversal_directly():
    """Appel direct du handler (sans route HTTP) pour garantir le guard is_relative_to."""
    _make_outer_secret()
    with pytest_raises_http():
        asyncio.run(main.serve_data_file("../guitarlab_secret.txt", _FakeRequest()))


def test_serve_file_handler_rejects_nonexistent_directly():
    with pytest_raises_http():
        asyncio.run(main.serve_data_file("missing.mp3", _FakeRequest()))


class _HTTPErrorContext:
    """Mini-contexte pour capturer HTTPException (équivalent pytest.raises)."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError("HTTPException attendue")
        assert exc_type is HTTPException and exc.status_code == 404
        return True


def pytest_raises_http():
    return _HTTPErrorContext()
