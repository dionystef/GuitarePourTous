"""Tests du service de fichiers ``GET /data/{file_path:path}``.

Travail supplémentaire par rapport aux tests existants : échappement par lien
symbolique (le ``.resolve()`` statique ne doit pas permettre de suivre un lien
vers l'extérieur de ``DATA_DIR``).
"""
from pathlib import Path

import main
from tests.helpers import run_app


def test_serve_file_rejects_symlink_escape(tmp_path):
    """Un lien symbolique DANS DATA_DIR pointant hors de DATA_DIR est refusé."""
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("SECRET_CONTENT")
    main.DATA_DIR.mkdir(parents=True, exist_ok=True)
    symlink = main.DATA_DIR / "t1" / "leak.mp3"
    symlink.parent.mkdir(parents=True, exist_ok=True)
    symlink.symlink_to(secret)

    async def _c(client):
        r = await client.get("/data/t1/leak.mp3")
        assert r.status_code == 404
        assert b"SECRET_CONTENT" not in r.content

    run_app(main.app, _c)


def test_serve_file_denies_traversal_then_returns_404(tmp_path):
    # Chemin parent (relatif) tentant de sortir : doit renvoyer 404.
    async def _c(client):
        r = await client.get("/data/../../etc/passwd")
        assert r.status_code == 404

    run_app(main.app, _c)


def test_serve_file_uses_path_not_string_prefix(tmp_path):
    """Le guard est ``is_relative_to``, pas un préfixe de chaîne fragile.

    Un répertoire frère dont le NOM partage le préfixe de ``DATA_DIR`` (ex.
    ``<data>_evil``) ne doit PAS être servi : avec un ``startswith`` il passerait
    la vérification, avec ``is_relative_to`` il est rejeté.
    """
    import asyncio

    sibling = tmp_path / (main.DATA_DIR.name + "_evil")
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "secret.txt").write_bytes(b"SECRET")

    class _H:
        def get(self, key, default=None):
            return default

    class _R:
        headers = _H()

    async def _invoke():
        # Chemin qui résout vers le dossier frère (hors DATA_DIR).
        rel = f"../../{sibling.name}/secret.txt"
        try:
            await main.serve_data_file(rel, _R())
        except Exception as exc:  # HTTPException attendue (404)
            from fastapi import HTTPException
            assert isinstance(exc, HTTPException) and exc.status_code == 404
            return
        raise AssertionError("HTTPException(404) attendue")

    asyncio.run(_invoke())
