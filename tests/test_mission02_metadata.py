r"""Tests MISSION-02 : métadonnées YouTube Music, recherche LRCLIB, filtre Whisper.

Couvre :
  * ``ingest_youtube`` : extraction title/artist/duration/thumbnail depuis la
    première entrée d'un flux album/playlist (repli sur ``info`` pour une vidéo
    simple, re-echo sur ``thumbnails[-1].url``), téléchargement best-effort de
    ``thumbnail.jpg`` et exposition ``/data/<id>/thumbnail.jpg`` (sinon None) ;
  * ``clean_title`` : retrait du préfixe ``^Album\s*[-–—:]\s*`` avant la
    normalisation des tirets (pas de fusion) ;
  * ``fetch_lrclib`` : l'artiste « Inconnu »/« Unknown » (insensible à la casse,
    trim) n'est pas envoyé à LRCLIB ;
  * ``lyrics_transcriber`` : filtre anti-hallucination Whisper (regex
    insensibles à la casse, textes partiels).

Tout est mocké (yt-dlp, LRCLIB, faster-whisper, urllib) : zéro réseau, zéro ffmpeg.
"""
import json
import sys
import types
from pathlib import Path

import pytest

import services.downloader as dl
import services.lyrics_provider as lp
import services.lyrics_transcriber as lt


# --------------------------------------------------------------------------- #
# clean_title : préfixe « Album »
# --------------------------------------------------------------------------- #
def test_clean_title_strips_album_prefix():
    assert lp.clean_title("Album - Song") == "Song"
    assert lp.clean_title("ALBUM : Song Title") == "Song Title"
    assert lp.clean_title("album—Song") == "Song"
    assert lp.clean_title("Album - My Song (Official Video)") == "My Song"


def test_clean_title_album_prefix_case_insensitive():
    assert lp.clean_title("album - lower") == "lower"
    assert lp.clean_title("ALBUM - UPPER") == "UPPER"
    assert lp.clean_title("Album - MiXeD") == "MiXeD"


def test_clean_title_album_prefix_dash_not_merged_into_title():
    """Le préfixe doit disparaître, PAS devenir « Album Song » (pas de fusion)."""
    assert lp.clean_title("Album - Song") != "Album Song"
    assert lp.clean_title("Album - Song - Remastered") == "Song Remastered"


def test_clean_title_no_prefix_unchanged_behavior():
    assert lp.clean_title("Song (Official Video)") == "Song"
    assert lp.clean_title("Song - Remastered [Clip]") == "Song Remastered"
    assert lp.clean_title("") == ""


def test_clean_title_album_prefix_only_leaves_empty():
    assert lp.clean_title("Album - ") == ""
    assert lp.clean_title("Album -") == ""


# --------------------------------------------------------------------------- #
# fetch_lrclib : artiste générique non transmis
# --------------------------------------------------------------------------- #
def _capture_lrclib(monkeypatch, artist_get=None, artist_search=None):
    """Monkeypatch les deux appels LRCLIB et capture les artistes envoyés."""
    calls = {}

    def fake_get(clean, artist, duration):
        calls["get_artist"] = artist
        calls["get_clean"] = clean
        return None  # force la voie search pour exercer les deux appels

    def fake_search(clean, artist):
        calls["search_artist"] = artist
        calls["search_clean"] = clean
        return "[00:00.00]Bonjour"

    monkeypatch.setattr(lp, "_get_synced_via_get", fake_get)
    monkeypatch.setattr(lp, "_get_synced_via_search", fake_search)
    return calls


def test_fetch_lrclib_omits_unknown_artist(monkeypatch):
    calls = _capture_lrclib(monkeypatch)
    res = lp.fetch_lrclib("Song", "Inconnu")
    assert res is not None
    assert calls["get_artist"] == ""  # « Inconnu » n'est pas envoyé
    assert calls["search_artist"] == ""


def test_fetch_lrclib_omits_unknown_artists_case_and_trim(monkeypatch):
    for bad in ("Inconnu", "Unknown", " INCONNU ", "unknown", "UnKnOwN"):
        calls = _capture_lrclib(monkeypatch)
        lp.fetch_lrclib("Song", bad)
        assert calls["get_artist"] == "", f"artiste {bad!r} doit être omis"


def test_fetch_lrclib_keeps_real_artist(monkeypatch):
    calls = _capture_lrclib(monkeypatch)
    lp.fetch_lrclib("Song", "AC/DC")
    assert calls["get_artist"] == "AC/DC"


# --------------------------------------------------------------------------- #
# _download_thumbnail : best-effort
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, data):
        self._data = data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._data


def test_download_thumbnail_writes_bytes(tmp_path, monkeypatch):
    dest = tmp_path / "thumbnail.jpg"
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=0: _FakeResp(b"\xff\xd8jpeg-bytes"),
    )
    dl._download_thumbnail("http://x/t.jpg", dest)
    assert dest.read_bytes() == b"\xff\xd8jpeg-bytes"


def test_download_thumbnail_failure_is_best_effort(tmp_path, monkeypatch):
    dest = tmp_path / "thumbnail.jpg"
    def boom(url, timeout=0):
        raise OSError("timeout")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    dl._download_thumbnail("http://x/t.jpg", dest)  # ne doit pas lever
    assert not dest.exists()


def test_download_thumbnail_empty_data_no_file(tmp_path, monkeypatch):
    dest = tmp_path / "thumbnail.jpg"
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=0: _FakeResp(b""))
    dl._download_thumbnail("http://x/t.jpg", dest)
    assert not dest.exists()


# --------------------------------------------------------------------------- #
# ingest_youtube : métadonnées album/playlist vs vidéo simple
# --------------------------------------------------------------------------- #
def _setup_ingest(tmp_path, monkeypatch, info):
    """Prépare un track_dir + mocks yt-dlp/ffmpeg/SSRF pour ``ingest_youtube``."""
    track_dir = tmp_path / "track_x"
    track_dir.mkdir(parents=True)

    monkeypatch.setattr(dl, "_reject_non_public", lambda url: None)
    monkeypatch.setattr(dl, "_to_wav", lambda src, dst: dst)
    monkeypatch.setattr(dl, "_to_mp3", lambda src, dst: dst)

    def _extract_info(url, download=False):
        source_dir = track_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "yt.mp3").write_bytes(b"dummy")
        return info

    class _FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            return _extract_info(url, download)

    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", _FakeYDL)
    return track_dir


def _entry(title="Piste 1", artist="Artiste1", duration=120, thumbnail="http://t/1.jpg"):
    return {"title": title, "artist": artist, "duration": duration,
            "thumbnail": thumbnail, "uploader": "Channel1"}


def test_ingest_youtube_album_uses_first_entry(tmp_path, monkeypatch):
    info = {
        "title": "Album Wrapper",
        "artist": "Album Artist",
        "thumbnails": [{"url": "http://x/album.jpg"}],
        "entries": [_entry("Première Piste", "Premier Artiste", 210, "http://t/first.jpg"),
                    _entry("Deuxième Piste", "Second Artiste", 180)],
    }
    track_dir = _setup_ingest(tmp_path, monkeypatch, info)
    monkeypatch.setattr(dl, "_download_thumbnail",
                        lambda url, dest: dest.write_bytes(b"img"))
    wav, meta = dl.ingest_youtube("https://youtube.com/watch?v=1", track_dir)
    assert meta["title"] == "Première Piste"
    assert meta["artist"] == "Premier Artiste"
    assert meta["duration"] == 210.0
    assert meta["thumbnail"] == f"/data/{track_dir.name}/thumbnail.jpg"
    assert (track_dir / "thumbnail.jpg").exists()


def test_ingest_youtube_simple_video_uses_info(tmp_path, monkeypatch):
    info = {"title": "Simple Title", "artist": "Solo Artist", "duration": 60,
            "thumbnail": "http://t/simple.jpg"}
    track_dir = _setup_ingest(tmp_path, monkeypatch, info)
    monkeypatch.setattr(dl, "_download_thumbnail",
                        lambda url, dest: dest.write_bytes(b"img"))
    wav, meta = dl.ingest_youtube("https://youtube.com/watch?v=2", track_dir)
    assert meta["title"] == "Simple Title"
    assert meta["artist"] == "Solo Artist"
    assert meta["duration"] == 60.0


def test_ingest_youtube_thumbnail_fallback_thumbnails_last(tmp_path, monkeypatch):
    # Pas de clé `thumbnail` : on reprend `thumbnails[-1].url`.
    info = {
        "title": "No Thumb Key", "artist": "X", "duration": 10,
        "thumbnails": [{"url": "http://t/0.jpg"}, {"url": "http://t/last.jpg"}],
    }
    track_dir = _setup_ingest(tmp_path, monkeypatch, info)
    captured = {}
    monkeypatch.setattr(dl, "_download_thumbnail",
                        lambda url, dest: captured.update(url=url) or dest.write_bytes(b"img"))
    dl.ingest_youtube("https://youtube.com/watch?v=3", track_dir)
    assert captured.get("url") == "http://t/last.jpg"


def test_ingest_youtube_download_failure_thumbnail_none(tmp_path, monkeypatch):
    """La miniature échouant ne fait pas échouer l'ingestion ; ``thumbnail`` = None."""
    info = {"title": "T", "artist": "A", "duration": 5, "thumbnail": "http://t/x.jpg"}
    track_dir = _setup_ingest(tmp_path, monkeypatch, info)
    monkeypatch.setattr(dl, "_download_thumbnail", lambda url, dest: None)  # ne crée pas le fichier
    wav, meta = dl.ingest_youtube("https://youtube.com/watch?v=4", track_dir)
    assert meta["thumbnail"] is None
    assert not (track_dir / "thumbnail.jpg").exists()


def test_ingest_youtube_thumbnail_none_when_no_source(tmp_path, monkeypatch):
    """Aucune info de miniature → thumbnail None, pas de fallback disque."""
    info = {"title": "T", "artist": "A", "duration": 5}
    track_dir = _setup_ingest(tmp_path, monkeypatch, info)
    monkeypatch.setattr(dl, "_download_thumbnail",
                        lambda url, dest: dest.write_bytes(b"img"))
    wav, meta = dl.ingest_youtube("https://youtube.com/watch?v=5", track_dir)
    assert meta["thumbnail"] is None


def test_ingest_upload_thumbnail_is_none(tmp_path, monkeypatch):
    """Pas de régression upload : la miniature de référence reste None."""
    raw = tmp_path / "moi.mp3"
    raw.write_bytes(b"xx")
    track_dir = tmp_path / "up"
    track_dir.mkdir()
    monkeypatch.setattr(dl, "_to_wav", lambda s, d: d)
    monkeypatch.setattr(dl, "_to_mp3", lambda s, d: d)
    monkeypatch.setattr(dl, "is_supported_audio", lambda p: True)
    monkeypatch.setattr(dl, "is_supported_audio_by_ext", lambda n: True)
    wav, meta = dl.ingest_upload(raw, track_dir)
    assert meta["thumbnail"] is None
    assert meta["source"] == "upload"


# --------------------------------------------------------------------------- #
# lyrics_transcriber : filtre anti-hallucination
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "Sous-titres réalisés par Amara.org",
    "amara.org subtitles",
    "Transcription réalisée par Whisper",
    "sous-titrage",
    "SOUS-TITRAGE",
    "credits sous-titres réalisés par quelqu'un",
])
def test_is_hallucinated_matches(text):
    assert lt._is_hallucinated(text) is True


@pytest.mark.parametrize("text", [
    "Amara",
    "sous-titres",
    "réalisés par Marie",
    "Je chante une chanson",
    "",
])
def test_is_hallucinated_does_not_match(text):
    assert lt._is_hallucinated(text) is False


def _patch_whisper_fallback(monkeypatch, segments):
    """Injecte un faster-whisper factice (CPU, sans réseau) pour les tests Whisper."""
    class _Info:
        language = "fr"

    class _Model:
        def transcribe(self, path, **k):
            return iter(segments), _Info()

    class _FakeWhisper:
        WhisperModel = lambda *a, **k: _Model()

    monkeypatch.setitem(sys.modules, "faster_whisper", _FakeWhisper)
    # torch stub : pas de CUDA → CPU/int8.
    torch_fake = types.ModuleType("torch")
    torch_fake.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch_fake)


class _FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def test_transcribe_vocals_fallback_filters_hallucinated_segments(tmp_path, monkeypatch):
    """La voie réelle du pipeline (``transcribe_vocals_fallback``) applique le filtre."""
    monkeypatch.setattr(lp, "MODELS_DIR", tmp_path / "models")
    segments = [
        _FakeSegment(0.0, 2.0, "Bonjour"),
        _FakeSegment(2.0, 4.0, "Sous-titres réalisés par Amara.org"),
        _FakeSegment(4.0, 6.0, "sous-titrage"),
        _FakeSegment(6.0, 8.0, ""),
        _FakeSegment(8.0, 10.0, "Je suis la vraie parole"),
    ]
    _patch_whisper_fallback(monkeypatch, segments)

    vocals = tmp_path / "vocals.mp3"
    vocals.write_bytes(b"x")
    lyrics = lp.transcribe_vocals_fallback(vocals)
    assert [l["text"] for l in lyrics] == ["Bonjour", "Je suis la vraie parole"]


def test_resolve_lyrics_whisper_filters_hallucinated_segments(tmp_path, monkeypatch):
    """``resolve_lyrics`` par la voie whisper doit exclure les segments hallucinés."""
    monkeypatch.setattr(lp, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(lp, "fetch_lrclib", lambda *a, **k: None)
    segments = [
        _FakeSegment(0.0, 2.0, "Bonjour"),
        _FakeSegment(2.0, 4.0, "Transcription réalisée par Whisper"),
        _FakeSegment(4.0, 6.0, "amara.org"),
        _FakeSegment(6.0, 8.0, "Je suis la vraie parole"),
    ]
    _patch_whisper_fallback(monkeypatch, segments)

    vocals = tmp_path / "vocals.mp3"
    vocals.write_bytes(b"x")
    out = tmp_path / "lyrics.json"
    lyrics, src = lp.resolve_lyrics("Ma Chanson", "Artiste", 120, vocals, out)
    assert src == "whisper"
    assert [l["text"] for l in lyrics] == ["Bonjour", "Je suis la vraie parole"]
    assert json.loads(out.read_text("utf-8")) == lyrics


def test_transcribe_vocals_filters_hallucinated_segments(tmp_path, monkeypatch):
    """Les segments hallucinés sont exclus ; seuls les paroles légitimes restent."""
    monkeypatch.setattr(lt, "MODELS_DIR", tmp_path / "models")

    class _Seg:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    segments = [_Seg(0.0, 2.0, "Bonjour"),
                _Seg(2.0, 4.0, "Sous-titres réalisés par Amara.org"),
                _Seg(4.0, 6.0, "transcription réalisée par X"),
                _Seg(6.0, 8.0, ""),
                _Seg(8.0, 10.0, "Je suis la vraie parole")]

    class _Info:
        language = "fr"

    class _Model:
        def transcribe(self, path, **k):
            return iter(segments), _Info()

    class _FakeWhisper:
        WhisperModel = lambda *a, **k: _Model()

    monkeypatch.setitem(sys.modules, "faster_whisper", _FakeWhisper)
    # torch stub : pas de CUDA → CPU/int8.
    torch_fake = types.ModuleType("torch")
    torch_fake.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch_fake)

    vocals = tmp_path / "vocals.mp3"
    vocals.write_bytes(b"x")
    out = tmp_path / "lyrics.json"

    lyrics = lt.transcribe_vocals(vocals, out)
    texts = [l["text"] for l in lyrics]
    assert texts == ["Bonjour", "Je suis la vraie parole"]
    # Le JSON écrit est cohérent.
    assert json.loads(out.read_text("utf-8")) == lyrics


# --------------------------------------------------------------------------- #
# Compléments MISSION-02 : cohérence du filtre + non-régression des voies
# --------------------------------------------------------------------------- #
def test_transcribe_vocals_fallback_case_insensitive_and_partial(monkeypatch, tmp_path):
    """La voie fallback filtre les segments hallucinés quelle que soit la casse
    et le texte (match partiel), tout en conservant les paroles légitimes."""
    monkeypatch.setattr(lp, "MODELS_DIR", tmp_path / "models")
    segments = [
        _FakeSegment(0.0, 2.0, "AMARA.ORG et autres crédits"),
        _FakeSegment(2.0, 4.0, "Sous-TITRes Réalisés Par Quelqu'un"),
        _FakeSegment(4.0, 6.0, "SOUS-TITRAGE"),
        _FakeSegment(6.0, 8.0, "La vraie chanson est là"),
        _FakeSegment(8.0, 10.0, "réalisés par Marie"),
    ]
    _patch_whisper_fallback(monkeypatch, segments)

    vocals = tmp_path / "vocals.mp3"
    vocals.write_bytes(b"x")
    lyrics = lp.transcribe_vocals_fallback(vocals)
    assert [l["text"] for l in lyrics] == ["La vraie chanson est là", "réalisés par Marie"]


def test_hallucination_filter_is_single_source_of_truth():
    """`lyrics_provider` et `lyrics_transcriber` partagent la MÊME fonction
    `_is_hallucinated` (aucune divergence de motifs entre les deux modules)."""
    assert lp._is_hallucinated is lt._is_hallucinated


def test_resolve_lyrics_lrclib_path_unaffected(monkeypatch, tmp_path):
    """Le câblage whisper ne doit pas casser la voie LRCLIB."""
    monkeypatch.setattr(lp, "fetch_lrclib", lambda *a, **k: [{"start": 0.0, "text": "une parole"}])
    out = tmp_path / "lyrics.json"
    lyrics, src = lp.resolve_lyrics("Titre", "Artiste", 100, None, out)
    assert src == "lrclib"
    assert lyrics == [{"start": 0.0, "text": "une parole"}]
    assert json.loads(out.read_text("utf-8")) == lyrics


def test_resolve_lyrics_none_path_unaffected(monkeypatch, tmp_path):
    """Sans paroles ni stem vocal, resolve_lyrics renvoie ( [], "none" )."""
    monkeypatch.setattr(lp, "fetch_lrclib", lambda *a, **k: None)
    out = tmp_path / "lyrics.json"
    lyrics, src = lp.resolve_lyrics("Titre", "", 0, None, out)
    assert src == "none"
    assert lyrics == []
