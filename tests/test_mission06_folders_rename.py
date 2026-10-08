r"""Tests MISSION-06 : dossiers virtuels + renommage de morceaux.

Couvre les points d'audit et de non-régression :

  * noms de dossiers réservés (``all`` / ``unclassified``, insensibles à la
    casse) → ``400`` sur ``POST /api/folders`` et ``PATCH /api/tracks/{id}`` ;
  * écritures ``metadata.json`` atomiques (aucun fichier ``*.tmp`` résiduel)
    dans ``update_track_meta`` (PATCH) et ``delete_folder`` (DELETE) ;
  * ``_summary`` expose bien le champ ``folder`` ;
  * ``GET /api/folders`` consolide ``folders.json`` et les dossiers déclarés
    dans les ``metadata.json``.
"""
import json

import main
from tests.helpers import run_app


def _mk_track(track_id: str, folder=None) -> str:
    """Crée un morceau minimal en place (``metadata.json`` + dossier)."""
    d = main.DATA_DIR / track_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": track_id,
        "title": f"Titre {track_id}",
        "artist": "Artiste",
        "created_at": "2026-10-06T00:00:00Z",
    }
    if folder is not None:
        meta["folder"] = folder
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return track_id


def _read_meta(track_id: str) -> dict:
    return json.loads((main.DATA_DIR / track_id / "metadata.json").read_text("utf-8"))


# --------------------------------------------------------------------------- #
# Noms de dossiers réservés (collision avec les onglets système)
# --------------------------------------------------------------------------- #
def test_post_folder_reserved_all_returns_400():
    """``POST /api/folders`` refuse ``all`` (insensible à la casse)."""
    async def _c(client):
        for name in ("all", "ALL", "All"):
            r = await client.post("/api/folders", data={"name": name})
            assert r.status_code == 400, name
    run_app(main.app, _c)


def test_post_folder_reserved_unclassified_returns_400():
    """``POST /api/folders`` refuse ``unclassified`` (insensible à la casse)."""
    async def _c(client):
        for name in ("unclassified", "UNCLASSIFIED", "Unclassified"):
            r = await client.post("/api/folders", data={"name": name})
            assert r.status_code == 400, name
    run_app(main.app, _c)


def test_post_folder_reserved_namespace_prefix_returns_400():
    """Le préfixe ``__`` (espace interne des onglets système) est réservé."""
    async def _c(client):
        for name in ("__all__", "__none__", "__Foo"):
            r = await client.post("/api/folders", data={"name": name})
            assert r.status_code == 400, name
    run_app(main.app, _c)


def test_patch_folder_reserved_returns_400_without_writing():
    """``PATCH /api/tracks/{id}`` refuse un dossier réservé et ne modifie rien."""
    tid = _mk_track("reserved_patch_1")

    async def _c(client):
        r = await client.patch(f"/api/tracks/{tid}", json={"folder": "all"})
        assert r.status_code == 400, r.text
        r = await client.patch(f"/api/tracks/{tid}", json={"folder": "unclassified"})
        assert r.status_code == 400, r.text
        r = await client.patch(f"/api/tracks/{tid}", json={"folder": "__all__"})
        assert r.status_code == 400, r.text
        # Aucune écriture : le dossier du morceau reste non classé.
        assert _read_meta(tid).get("folder") is None

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# Écritures atomiques de metadata.json
# --------------------------------------------------------------------------- #
def test_patch_metadata_atomic_no_tmp_file():
    """Le PATCH écrit ``metadata.json`` atomiquement (aucun ``*.tmp`` résiduel)."""
    tid = _mk_track("atomic_patch_1")

    async def _c(client):
        r = await client.patch(f"/api/tracks/{tid}",
                               json={"title": "  Nouveau titre  ",
                                     "artist": "  Nouvel artiste  "})
        assert r.status_code == 200, r.text
        leftovers = [p.name for p in (main.DATA_DIR / tid).glob("*.tmp")]
        assert leftovers == [], leftovers
        meta = _read_meta(tid)
        assert meta["title"] == "Nouveau titre"
        assert meta["artist"] == "Nouvel artiste"

    run_app(main.app, _c)


def test_delete_folder_metadata_atomic_no_tmp_file():
    """Le DELETE de dossier déclasse les morceaux via une écriture atomique."""
    tid = _mk_track("atomic_del_1", folder="Pop")

    async def _c(client):
        r = await client.delete("/api/folders/Pop")
        assert r.status_code == 200, r.text
        assert r.json()["removed_tracks"] == 1
        leftovers = [p.name for p in (main.DATA_DIR / tid).glob("*.tmp")]
        assert leftovers == [], leftovers
        assert _read_meta(tid).get("folder") is None

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# _summary & GET /api/folders
# --------------------------------------------------------------------------- #
def test_summary_exposes_folder():
    """``GET /api/tracks/{id}`` (via ``_summary``) expose le champ ``folder``."""
    tid = _mk_track("summary_folder_1", folder="Jazz")

    async def _c(client):
        r = await client.get(f"/api/tracks/{tid}")
        assert r.status_code == 200
        body = r.json()
        assert body["folder"] == "Jazz"
        assert "folder" in body

    run_app(main.app, _c)


def test_get_folders_consolidates_meta_and_manifest():
    """``GET /api/folders`` unit ``folders.json`` et les dossiers de métadonnées."""
    _mk_track("get_folders_meta_only", folder="MetaOnly")
    # POST crée un dossier persistant dans folders.json.
    async def _c(client):
        r = await client.post("/api/folders", data={"name": "ManifestOnly"})
        assert r.status_code == 200
        r = await client.get("/api/folders")
        assert r.status_code == 200
        folders = r.json()
        assert "MetaOnly" in folders
        assert "ManifestOnly" in folders
        assert folders == sorted(folders)

    run_app(main.app, _c)
