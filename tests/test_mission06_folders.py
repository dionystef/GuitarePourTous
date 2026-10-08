r"""Tests MISSION-06 : renommage titre/artiste + dossiers virtuels.

Couvre la logique pure et les endpoints introduits pour la bibliothèque :

  * ``_validate_folder_name`` : validation / nettoyage des noms de dossier ;
  * ``_load_folders`` / ``_save_folders`` : persistance ``folders.json``
    (fichier absent / corrompu / forme liste ou dict, écriture normalisée) ;
  * ``_tracks_folders_from_meta`` : consolidation des dossiers déclarés par les
    ``metadata.json`` des morceaux (assignation directe via le sélecteur) ;
  * ``_summary`` : le champ ``folder`` est bien exposé ;
  * ``PATCH /api/tracks/{id}`` : mise à jour title/artist/folder (404/400/
    dossier vide = déclasse) ;
  * ``GET /api/folders`` : liste consolidée et triée ;
  * ``POST /api/folders`` : création, déduplication, refus des noms invalides ;
  * ``DELETE /api/folders/{name}`` : suppression + déclassement des morceaux.

Chaque test utilise un ``DATA_DIR`` temporaire isolé (fixture ``data``) pour ne
pas se contaminer entre fichiers de test partageant le DATA_DIR de session.
Tout passe par ``run_app`` (ASGI, zéro réseau).
"""
import json

import pytest

import main
from fastapi import HTTPException
from tests.helpers import run_app


@pytest.fixture
def data(monkeypatch, tmp_path):
    """Répertoire de données isolé par test (``main.DATA_DIR`` redirigé)."""
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    return tmp_path


def _write_track(data, track_id, **meta):
    """Crée un morceau (``<data>/<id>/metadata.json``) et en renvoie le métadata."""
    d = data / track_id
    d.mkdir(parents=True, exist_ok=True)
    base = {"id": track_id, "title": "Titre", "artist": "Artiste",
            "duration": 120.0, "status": "ready", "folder": None}
    base.update(meta)
    (d / "metadata.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return base


# --------------------------------------------------------------------------- #
# _summary : le champ folder doit être exposé
# --------------------------------------------------------------------------- #
def test_summary_includes_folder():
    s = main._summary({"id": "x", "title": "T", "artist": "A", "folder": "Rock",
                       "stems": [], "files": {}, "status": "ready"})
    assert s["folder"] == "Rock"


# --------------------------------------------------------------------------- #
# _validate_folder_name
# --------------------------------------------------------------------------- #
def test_validate_folder_name_ok():
    assert main._validate_folder_name("Rock") == "Rock"
    assert main._validate_folder_name("  Jazz  ") == "Jazz"
    assert main._validate_folder_name("R&B") == "R&B"
    assert main._validate_folder_name("x" * 100) == "x" * 100  # limite exacte


def test_validate_folder_name_empty_rejects():
    for bad in ("", "   "):
        with pytest.raises(HTTPException) as exc:
            main._validate_folder_name(bad)
        assert exc.value.status_code == 400


def test_validate_folder_name_too_long_rejects():
    with pytest.raises(HTTPException) as exc:
        main._validate_folder_name("x" * 101)
    assert exc.value.status_code == 400


def test_validate_folder_name_control_chars_rejects():
    for bad in ("a\nb", "a\tb", "a\x00b", "a\rb"):
        with pytest.raises(HTTPException) as exc:
            main._validate_folder_name(bad)
        assert exc.value.status_code == 400, repr(bad)


def test_validate_folder_name_path_separator_rejects():
    for bad in ("Rock/Jazz", "Rock\\Jazz", "/Rock", "Rock/"):
        with pytest.raises(HTTPException) as exc:
            main._validate_folder_name(bad)
        assert exc.value.status_code == 400, repr(bad)


# --------------------------------------------------------------------------- #
# _load_folders / _save_folders
# --------------------------------------------------------------------------- #
def test_load_folders_missing_file(data):
    assert main._load_folders() == []


def test_load_folders_corrupt_file(data):
    (data / "folders.json").write_text("{pas du json", encoding="utf-8")
    assert main._load_folders() == []


def test_load_folders_list_form(data):
    (data / "folders.json").write_text(
        json.dumps(["Rock", "  Jazz  ", "", "   ", "Folk"]), encoding="utf-8")
    assert main._load_folders() == ["Rock", "Jazz", "Folk"]


def test_load_folders_dict_form(data):
    # Format toléré pour une évolution future : {"Rock": true, ...}.
    (data / "folders.json").write_text(
        json.dumps({"Rock": True, "Jazz": True, "": True}), encoding="utf-8")
    assert main._load_folders() == ["Rock", "Jazz"]


def test_load_folders_unexpected_type(data):
    (data / "folders.json").write_text(json.dumps("Rock"), encoding="utf-8")
    assert main._load_folders() == []


def test_save_folders_normalizes_and_persists(data):
    main._save_folders(["Rock", "  Jazz  ", "", "   "])
    assert json.loads((data / "folders.json").read_text("utf-8")) == ["Rock", "Jazz"]


def test_save_folders_atomic_no_tmp_leftover(data):
    main._save_folders(["Rock", "Jazz"])
    assert (data / "folders.json").exists()
    assert (data / "folders.json.tmp").exists() is False


def test_save_folders_roundtrip(data):
    main._save_folders(["Rock"])
    assert main._load_folders() == ["Rock"]


# --------------------------------------------------------------------------- #
# _tracks_folders_from_meta (consolidation depuis les metadata)
# --------------------------------------------------------------------------- #
def test_tracks_folders_from_meta_consolidates_and_trims(data):
    _write_track(data, "t1", folder="Rock")
    _write_track(data, "t2", folder=" Jazz ")   # l'espace doit être trimé
    _write_track(data, "t3", folder=None)
    _write_track(data, "t4", folder="")          # vide => ignoré
    assert main._tracks_folders_from_meta() == {"Rock", "Jazz"}


def test_tracks_folders_from_meta_skips_corrupt_metadata(data):
    _write_track(data, "t1", folder="Rock")
    (data / "t2").mkdir()
    (data / "t2" / "metadata.json").write_text("{corrompu", encoding="utf-8")
    assert main._tracks_folders_from_meta() == {"Rock"}


def test_tracks_folders_from_meta_empty(data):
    assert main._tracks_folders_from_meta() == set()


# --------------------------------------------------------------------------- #
# PATCH /api/tracks/{id}
# --------------------------------------------------------------------------- #
def test_patch_updates_title_and_artist(data):
    _write_track(data, "tA", title="Avant", artist="Artiste1", folder=None)

    async def _c(client):
        r = await client.patch("/api/tracks/tA",
                               json={"title": "Après", "artist": "Nouveau"})
        assert r.status_code == 200
        b = r.json()
        assert b["title"] == "Après"
        assert b["artist"] == "Nouveau"
        assert b["folder"] is None
        # le dict original est bien mis à jour (titre/artiste)
        m = json.loads((data / "tA" / "metadata.json").read_text("utf-8"))
        assert m["title"] == "Après"
        assert m["artist"] == "Nouveau"

    run_app(main.app, _c)


def test_patch_updates_folder_keeps_other_fields(data):
    _write_track(data, "tB", title="Titre", artist="Artiste")

    async def _c(client):
        r = await client.patch("/api/tracks/tB", json={"folder": "Rock"})
        assert r.status_code == 200
        b = r.json()
        assert b["folder"] == "Rock"
        assert b["title"] == "Titre"
        assert b["artist"] == "Artiste"

    run_app(main.app, _c)


def test_patch_empty_folder_unclassifies(data):
    _write_track(data, "tC", folder="Rock")

    async def _c(client):
        r = await client.patch("/api/tracks/tC", json={"folder": "   "})
        assert r.status_code == 200
        assert r.json()["folder"] is None
        m = json.loads((data / "tC" / "metadata.json").read_text("utf-8"))
        assert m["folder"] is None

    run_app(main.app, _c)


def test_patch_unknown_track_404(data):
    async def _c(client):
        r = await client.patch("/api/tracks/inconnu_20260101-120000",
                               json={"title": "X"})
        assert r.status_code == 404

    run_app(main.app, _c)


def test_patch_traversal_id_404(data):
    # Identifiant mono-segment invalide : le guard `_safe_track_id` doit répondre 404.
    async def _c(client):
        r = await client.patch("/api/tracks/.hidden", json={"title": "X"})
        assert r.status_code == 404

    run_app(main.app, _c)


def test_patch_bad_payload_400(data):
    _write_track(data, "tE", title="Titre")

    async def _c(client):
        # Corps non-objet (liste) : rejeté par le modèle Pydantic → 400.
        r = await client.patch("/api/tracks/tE", json=["nul", "objet"])
        assert r.status_code == 400

    run_app(main.app, _c)


def test_patch_empty_title_400(data):
    _write_track(data, "tF", title="Titre")

    async def _c(client):
        r = await client.patch("/api/tracks/tF", json={"title": "   "})
        assert r.status_code == 400

    run_app(main.app, _c)


def test_patch_invalid_folder_name_400(data):
    _write_track(data, "tG", title="Titre")

    async def _c(client):
        r = await client.patch("/api/tracks/tG", json={"folder": "Rock/Jazz"})
        assert r.status_code == 400
        r2 = await client.patch("/api/tracks/tG", json={"folder": "a\\b"})
        assert r2.status_code == 400

    run_app(main.app, _c)


def test_patch_partial_update_keeps_untouched_fields(data):
    _write_track(data, "tH", title="Titre", artist="Artiste", folder="Rock")

    async def _c(client):
        r = await client.patch("/api/tracks/tH", json={"title": "Nouveau"})
        assert r.status_code == 200
        b = r.json()
        assert b["title"] == "Nouveau"
        assert b["artist"] == "Artiste"
        assert b["folder"] == "Rock"

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# GET /api/folders (liste consolidée)
# --------------------------------------------------------------------------- #
def test_list_folders_empty(data):
    async def _c(client):
        r = await client.get("/api/folders")
        assert r.status_code == 200
        assert r.json() == []

    run_app(main.app, _c)


def test_list_folders_consolidates_and_sorts(data):
    _write_track(data, "t1", folder="Rock")
    _write_track(data, "t2", folder="Blues")
    _write_track(data, "t3", folder=None)
    # Un dossier sans aucun morceau, mais présent dans folders.json.
    main._save_folders(["Jazz"])

    async def _c(client):
        r = await client.get("/api/folders")
        assert r.status_code == 200
        assert r.json() == ["Blues", "Jazz", "Rock"]

    run_app(main.app, _c)


def test_list_folders_ignores_corrupt_metadata(data):
    (data / "bizarre").mkdir()
    (data / "bizarre" / "metadata.json").write_text("{broken", encoding="utf-8")
    _write_track(data, "t1", folder="Rock")

    async def _c(client):
        r = await client.get("/api/folders")
        assert r.status_code == 200
        assert r.json() == ["Rock"]

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# POST /api/folders
# --------------------------------------------------------------------------- #
def test_create_folder_persists(data):
    async def _c(client):
        r = await client.post("/api/folders", data={"name": "  Rock  "})
        assert r.status_code == 200
        b = r.json()
        assert b["ok"] is True
        assert b["name"] == "Rock"
        assert json.loads((data / "folders.json").read_text("utf-8")) == ["Rock"]

    run_app(main.app, _c)


def test_create_folder_no_duplicate(data):
    async def _c(client):
        r1 = await client.post("/api/folders", data={"name": "Rock"})
        assert r1.status_code == 200
        r2 = await client.post("/api/folders", data={"name": "Rock"})
        assert r2.status_code == 200
        assert r2.json()["folders"] == ["Rock"]
        assert json.loads((data / "folders.json").read_text("utf-8")) == ["Rock"]

    run_app(main.app, _c)


def test_create_folder_preserves_order_and_adds(data):
    async def _c(client):
        await client.post("/api/folders", data={"name": "Rock"})
        r = await client.post("/api/folders", data={"name": "Jazz"})
        assert r.json()["folders"] == ["Jazz", "Rock"]

    run_app(main.app, _c)


def test_create_folder_invalid_name_400(data):
    async def _c(client):
        for bad in ("", "   ", "a/b", "a\\b", "x" * 101, "a\nb"):
            r = await client.post("/api/folders", data={"name": bad})
            assert r.status_code == 400, repr(bad)

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# DELETE /api/folders/{name}
# --------------------------------------------------------------------------- #
def test_delete_folder_unclassifies_tracks(data):
    _write_track(data, "t1", folder="Rock")
    _write_track(data, "t2", folder="Rock")
    _write_track(data, "t3", folder="Blues")
    main._save_folders(["Rock", "Blues"])

    async def _c(client):
        r = await client.delete("/api/folders/Rock")
        assert r.status_code == 200
        b = r.json()
        assert b["ok"] is True
        assert b["removed_tracks"] == 2
        assert b["folders"] == ["Blues"]
        m1 = json.loads((data / "t1" / "metadata.json").read_text("utf-8"))
        assert m1["folder"] is None
        m3 = json.loads((data / "t3" / "metadata.json").read_text("utf-8"))
        assert m3["folder"] == "Blues"

    run_app(main.app, _c)


def test_delete_folder_removes_from_consolidated_listing(data):
    _write_track(data, "t1", folder="Rock")
    main._save_folders(["Rock"])

    async def _c(client):
        r = await client.delete("/api/folders/Rock")
        assert r.status_code == 200
        assert r.json()["removed_tracks"] == 1
        r2 = await client.get("/api/folders")
        assert r2.json() == []

    run_app(main.app, _c)


def test_delete_folder_not_in_folders_json_still_unclassifies(data):
    # Un dossier assigné via le sélecteur sans figurer dans folders.json.
    _write_track(data, "tX", folder="Orpheline")

    async def _c(client):
        r = await client.delete("/api/folders/Orpheline")
        assert r.status_code == 200
        assert r.json()["removed_tracks"] == 1
        m = json.loads((data / "tX" / "metadata.json").read_text("utf-8"))
        assert m["folder"] is None

    run_app(main.app, _c)


def test_delete_folder_untouched_tracks_kept(data):
    _write_track(data, "t1", folder="Rock")
    _write_track(data, "t2", folder="Jazz")
    main._save_folders(["Rock"])

    async def _c(client):
        r = await client.delete("/api/folders/Rock")
        assert r.status_code == 200
        m2 = json.loads((data / "t2" / "metadata.json").read_text("utf-8"))
        assert m2["folder"] == "Jazz"

    run_app(main.app, _c)


def test_delete_folder_blank_name_400(data):
    async def _c(client):
        # Nom composé d'espaces après décodage (`%20`) → strip() = "" → 400.
        r = await client.delete("/api/folders/%20")
        assert r.status_code == 400

    run_app(main.app, _c)


# --------------------------------------------------------------------------- #
# MISSION-06-audit : réservation des noms de dossiers (all/unclassified, __)
# --------------------------------------------------------------------------- #
def test_validate_folder_name_reserved_names_rejected():
    """Les noms réservés aux onglets système sont refusés (insensibles à la casse)."""
    for bad in ("all", "ALL", "All", "unclassified", "UNCLASSIFIED", "Unclassified"):
        with pytest.raises(HTTPException) as exc:
            main._validate_folder_name(bad)
        assert exc.value.status_code == 400, repr(bad)


def test_validate_folder_name_underscore_prefix_rejected():
    """Le préfixe `__` (espace interne des onglets système) est réservé."""
    for bad in ("__all__", "__none__", "__Foo", "__ "):
        with pytest.raises(HTTPException) as exc:
            main._validate_folder_name(bad)
        assert exc.value.status_code == 400, repr(bad)


def test_is_reserved_folder_name_reserved_names():
    """`_is_reserved_folder_name` écarte `all`/`unclassified` (insensibles à la casse)."""
    for bad in ("all", "ALL", "All", "unclassified", "UNCLASSIFIED", "Unclassified"):
        assert main._is_reserved_folder_name(bad) is True, repr(bad)
    # Un nom réservé entouré d'espaces est reconnu après trim.
    assert main._is_reserved_folder_name("  all  ") is True
    assert main._is_reserved_folder_name(" unclassified ") is True


def test_is_reserved_folder_name_underscore_prefix():
    """`_is_reserved_folder_name` écarte tout préfixe `__` (espace interne)."""
    for bad in ("__all__", "__none__", "__Foo", "__x", "__"):
        assert main._is_reserved_folder_name(bad) is True, repr(bad)


def test_is_reserved_folder_name_allows_normal_names():
    """Les noms de dossiers légitimes ne sont pas écartés."""
    for ok in ("Rock", "Jazz", "R&B", "x" * 100, "_single", "a_b", "alligator"):
        assert main._is_reserved_folder_name(ok) is False, repr(ok)
    # Vide / blanc : la fonction renvoie False (le filtrage amont gère le vide).
    assert main._is_reserved_folder_name("") is False
    assert main._is_reserved_folder_name("   ") is False
    assert main._is_reserved_folder_name(None) is False


def test_tracks_folders_from_meta_excludes_reserved_and_prefix(data):
    """Les dossiers réservés / préfixés `__` dans les metadata sont ignorés."""
    _write_track(data, "t1", folder="all")
    _write_track(data, "t2", folder="UNCLASSIFIED")
    _write_track(data, "t3", folder="__x")
    _write_track(data, "t4", folder="Rock")
    _write_track(data, "t5", folder="Jazz")
    assert main._tracks_folders_from_meta() == {"Rock", "Jazz"}


def test_get_folders_excludes_reserved_metadata_folders(data):
    """Un morceau affecté à un dossier réservé ne doit pas créer d'onglet."""
    _write_track(data, "t1", folder="all")
    _write_track(data, "t2", folder="unclassified")
    _write_track(data, "t3", folder="__all__")
    _write_track(data, "t4", folder="Rock")

    async def _c(client):
        r = await client.get("/api/folders")
        assert r.status_code == 200
        assert r.json() == ["Rock"]

    run_app(main.app, _c)


def test_get_folders_filters_reserved_from_manifest(data):
    """Un dossier réservé hérité dans folders.json ne doit pas créer d'onglet.

    Cas de données historiques (créé avant la réservation) : le manifeste ne
    doit jamais renvoyer un nom qui entrerait en collision avec les onglets
    système (`__all__`/`__none__`) ou qui est réservé.
    """
    # folders.json contient des noms réservés + un nom légitime.
    main._save_folders(["all", "unclassified", "__all__", "__x", "Rock"])
    _write_track(data, "t1", folder="Rock")

    async def _c(client):
        r = await client.get("/api/folders")
        assert r.status_code == 200
        assert r.json() == ["Rock"]

    run_app(main.app, _c)
