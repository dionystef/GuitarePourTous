"""Tests des fonctions d'identifiants de morceaux (slug + validation anti-traversal)."""
import re

import main
from services.library import _safe_track_id


def test_slugify_nominal():
    assert main._slugify("Hello World") == "hello-world"


def test_slugify_accents_and_specials():
    # NFKD → ASCII : les accents sont perdus, les symboles deviennent des tirets.
    assert main._slugify("  ÀÉÎ chanson -- extrait  ") == "aei-chanson-extrait"


def test_slugify_empty_and_blank():
    assert main._slugify("") == "titre"
    assert main._slugify("   ") == "titre"


def test_slugify_maxlen():
    long_title = "a" * 200
    slug = main._slugify(long_title, maxlen=60)
    assert len(slug) <= 60
    assert slug == "a" * 60


def test_track_id_for_shape():
    tid = main._track_id_for("Ma Chanson")
    assert re.match(r"^[a-z0-9-]+_\d{8}-\d{6}$", tid)


def test_safe_track_id_valid():
    assert _safe_track_id("ma_chanson_20250101-120000") is True
    assert _safe_track_id("abc-123") is True
    assert _safe_track_id("D5") is True


def test_safe_track_id_rejects_traversal():
    assert _safe_track_id("../etc") is False
    assert _safe_track_id("..") is False
    assert _safe_track_id(".") is False
    assert _safe_track_id("a/b") is False
    assert _safe_track_id("a\\b") is False
    assert _safe_track_id("/abs") is False
    assert _safe_track_id(".hidden") is False
    assert _safe_track_id("") is False
