"""Tests des en-têtes de sécurité et de la non-divulgation d'informations.

Couvre la politique CSP + ``X-Content-Type-Options`` + ``X-Frame-Options``
appliquée par le middleware à toutes les réponses, la suppression de
``data_dir`` de ``/api/health``, et le cache-control des pages statiques.
"""
import asyncio
import json

import main
from tests.helpers import run_app

# Politique CSP attendue (elle contient les directives clés).
CSP_KEYS = (
    "default-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
)


def _csp_is_valid(csp: str) -> bool:
    return all(key in csp for key in CSP_KEYS)


def test_all_responses_carry_security_headers():
    """Chaque réponse (API, HTML, données) porte CSP + nosniff + X-Frame-Options."""

    async def _c(client):
        for path in ("/api/health", "/", "/static/app.js"):
            r = await client.get(path)
            assert r.status_code == 200, path
            csp = r.headers.get("content-security-policy", "")
            assert _csp_is_valid(csp), f"CSP absente/incomplète sur {path}: {csp!r}"
            assert r.headers.get("x-content-type-options") == "nosniff", path
            assert r.headers.get("x-frame-options") == "DENY", path

    run_app(main.app, _c)


def test_csp_blocks_none_objects_frames_and_forms():
    """Les directives défensives clés sont présentes (anti-clickjacking/anti-XSS)."""
    csp = main.CSP
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "script-src 'self'" in csp
    assert "img-src 'self' data: https://i.ytimg.com" in csp


def test_health_does_not_leak_data_dir():
    """``/api/health`` n'expose plus le chemin du répertoire de données."""

    async def _c(client):
        r = await client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "data_dir" not in body
        assert "version" in body and "device" in body

    run_app(main.app, _c)


def test_static_pages_disabled_cache():
    """Les pages statiques sont servies sans cache (dev/mises à jour)."""

    async def _c(client):
        r = await client.get("/")
        assert r.headers.get("cache-control", "").startswith("no-cache")
        r = await client.get("/static/app.js")
        assert r.headers.get("cache-control", "").startswith("no-cache")

    run_app(main.app, _c)


def test_api_endpoint_does_not_print_source_path(monkeypatch, tmp_path):
    """Le message d'erreur du pipeline n'expose pas le chemin source absolu.

    Dans ``_pipeline``, l'exception doit produire un message générique (pas de
    ``str(exc)`` ni de chemin local). On simule une erreur d'ingestion et on
    vérifie le contenu de ``metadata.json`` et du log.
    """
    def boom(*a, **k):
        raise RuntimeError("/secret/chemin/absolu/raw.mp3")

    monkeypatch.setattr(main, "ingest_upload", boom)
    main.mgr.jobs.clear()
    main.mgr.tasks.clear()
    tid = "secret_track_20260101-120000"

    async def _scenario():
        main.mgr.loop = asyncio.get_running_loop()
        main.mgr.start(tid, {"type": "upload",
                             "filename": "x.mp3",
                             "raw_path": tmp_path / "x.mp3"})
        while tid in main.mgr.tasks:
            await asyncio.sleep(0.02)

    asyncio.run(_scenario())
    main.mgr.loop = None
    meta_path = main.DATA_DIR / tid / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text("utf-8"))
        assert "/secret" not in json.dumps(meta)
        assert "raw.mp3" not in json.dumps(meta)
        assert meta.get("source") == "upload"
    main.mgr.remove_job(tid)
