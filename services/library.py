"""Services de la bibliothèque locale — gestion des morceaux dans ``data/``.

Chaque morceau correspond à un répertoire ``<data>/<track_id>/`` contenant
``metadata.json``, ``status.json`` et tous les artefacts produits
(stems, grille d'accords, MIDI, tablature).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("guitarlab.library")

HERE = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))


def _safe_track_id(track_id: str) -> bool:
    """Interdit les identifiants capables de sortir de la bibliothèque."""
    if not track_id or track_id in (".", ".."):
        return False
    if "/" in track_id or "\\" in track_id or track_id.startswith("."):
        return False
    return True


def delete_track(track_id: str) -> bool:
    """Supprime récursivement ``<data>/<track_id>``.

    Retourne ``True`` si le dossier existait et a été supprimé, ``False``
    dans le cas contraire (identifiant invalide ou morceau introuvable).
    """
    if not _safe_track_id(track_id):
        logger.info("Suppression refusée : identifiant invalide (%r)", track_id)
        return False
    target = DATA_DIR / track_id
    if not target.exists():
        logger.info("Suppression demandée pour %s : introuvable", track_id)
        return False
    try:
        shutil.rmtree(target, ignore_errors=False)
    except OSError as exc:
        logger.error("Suppression de %s en échec : %s", track_id, exc)
        return False
    logger.info("Morceau %s supprimé", track_id)
    return True