r"""Tests MISSION : moteur de mastering audio.

Couvre ``services/mastering.py`` (logique pure + pipeline ``sum_stems`` /
``apply_mastering``) et les endpoints exposés dans ``main.py`` :

  * ``GET /api/mastering/presets`` — liste des presets ;
  * ``POST /api/tracks/{id}/master`` — validation, somme des stems, calibrage
    Matchering, écriture de ``master.wav`` et mise à jour de ``files.master``.

Les tests du service sont indépendants de ``main`` (hors endpoints). Les tests
d'endpoints passent par ``run_app`` (ASGI, zéro réseau) et isolent ``DATA_DIR``
par test. La fréquence d'échantillonnage et la durée restent petites pour des
exécutions rapides ; l'appel à Matchering n'est déclenché que sur le chemin
nominal (et une fois via la génération de référence de repli).
"""
import json
import math

import numpy as np
import pytest
import soundfile as sf

import main
import services.mastering as mastering
from tests.helpers import run_app


# --------------------------------------------------------------------------- #
# Auxiliaires
# --------------------------------------------------------------------------- #
_SR = 44100


def _write_stem(path, sr, data, subtype="PCM_16"):
    """Écrit un fichier audio (créant le dossier parent au besoin)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, sr, subtype=subtype)


def _noise_stereo(sr=_SR, duration=1.0, amp=0.3, seed=1):
    """Bruit blanc stéréo (large bande : le passe-haut le laisse quasi intact)."""
    n = int(sr * duration)
    rng = np.random.default_rng(seed)
    left = amp * rng.standard_normal(n).astype(np.float32)
    right = amp * rng.standard_normal(n).astype(np.float32)
    return np.stack([left, right], axis=1)


# --------------------------------------------------------------------------- #
# Presets / normalisation
# --------------------------------------------------------------------------- #
def test_get_presets_returns_ordered_list():
    assert mastering.get_presets() == ["standard", "rock", "acoustic"]


def test_get_presets_returns_a_copy():
    presets = mastering.get_presets()
    presets.append("bogus")
    assert mastering.get_presets() == ["standard", "rock", "acoustic"]


def test_normalize_preset_accepts_known_names():
    assert mastering._normalize_preset("standard") == "standard"
    assert mastering._normalize_preset("rock") == "rock"
    assert mastering._normalize_preset("acoustic") == "acoustic"


def test_normalize_preset_is_case_and_space_insensitive():
    assert mastering._normalize_preset("  Rock  ") == "rock"
    assert mastering._normalize_preset("ACOUSTIC") == "acoustic"


def test_normalize_preset_falls_back_to_standard():
    assert mastering._normalize_preset("jazz") == "standard"
    assert mastering._normalize_preset("") == "standard"
    assert mastering._normalize_preset(None) == "standard"


# --------------------------------------------------------------------------- #
# _validate_volume
# --------------------------------------------------------------------------- #
def test_validate_volume_accepts_finite_values():
    assert mastering._validate_volume("vocals", 1.0) == 1.0
    assert mastering._validate_volume("drums", 0.5) == 0.5
    assert mastering._validate_volume("guitar", "-2.0") == -2.0  # str converti


def test_validate_volume_rejects_non_finite_values():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(mastering.InvalidVolumeError):
            mastering._validate_volume("vocals", bad)


def test_validate_volume_rejects_magnitude_above_max_gain():
    # Gain fini mais absurde (> MAX_GAIN) : rejeté pour éviter un débordement.
    for bad in (mastering.MAX_GAIN + 1e-9, mastering.MAX_GAIN * 2, -mastering.MAX_GAIN * 2):
        with pytest.raises(mastering.InvalidVolumeError):
            mastering._validate_volume("vocals", bad)
    # La borne exacte reste acceptée.
    assert mastering._validate_volume("vocals", mastering.MAX_GAIN) == mastering.MAX_GAIN


# --------------------------------------------------------------------------- #
# _generate_reference_wav (étalon de repli)
# --------------------------------------------------------------------------- #
def test_generate_reference_wav_writes_stereo_peaked(tmp_path):
    out = tmp_path / "ref.wav"
    mastering._generate_reference_wav(out, "standard", sample_rate=44100, duration=0.5)
    data, sr = sf.read(str(out), dtype="float32")
    assert data.ndim == 2 and data.shape[1] == 2
    assert sr == 44100
    assert abs(float(np.max(np.abs(data))) - 0.6) < 1e-3


def test_generate_reference_wav_is_deterministic(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    mastering._generate_reference_wav(a, "rock", sample_rate=22050, duration=0.4)
    mastering._generate_reference_wav(b, "rock", sample_rate=22050, duration=0.4)
    da, _ = sf.read(str(a), dtype="float32")
    db, _ = sf.read(str(b), dtype="float32")
    np.testing.assert_array_equal(da, db)


def test_generate_reference_wav_accepts_unknown_preset_shape(tmp_path):
    # Un preset inconnu retombe sur le gabarit « standard » sans lever d'erreur.
    out = tmp_path / "ref.wav"
    mastering._generate_reference_wav(out, "nope", sample_rate=22050, duration=0.3)
    data, sr = sf.read(str(out), dtype="float32")
    assert sr == 22050 and data.ndim == 2


# --------------------------------------------------------------------------- #
# _pad_to
# --------------------------------------------------------------------------- #
def test_pad_to_duplicates_mono_to_stereo():
    mono = np.ones((10, 1), dtype=np.float32)
    out = mastering._pad_to(mono, 10, 2)
    assert out.shape == (10, 2)
    np.testing.assert_array_equal(out[:, 0], out[:, 1])


def test_pad_to_pads_frames_and_channels():
    data = np.ones((5, 1), dtype=np.float32)
    out = mastering._pad_to(data, 8, 2)
    assert out.shape == (8, 2)
    assert out[5:, :].sum() == 0.0          # trames ajoutées = silence
    assert np.all(out[0, 0] == 1.0)


def test_pad_to_truncates_extra_channels():
    data = np.ones((4, 3), dtype=np.float32)
    out = mastering._pad_to(data, 4, 2)
    assert out.shape == (4, 2)
    np.testing.assert_array_equal(out, np.ones((4, 2), dtype=np.float32))


def test_pad_to_does_not_mutate_input():
    data = np.ones((5, 1), dtype=np.float32)
    mastering._pad_to(data, 8, 2)
    assert data.shape == (5, 1)
    assert np.all(data == 1.0)


# --------------------------------------------------------------------------- #
# _resolve_reference
# --------------------------------------------------------------------------- #
def test_resolve_reference_generates_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mastering, "REFERENCE_DIR", tmp_path)
    ref = mastering._resolve_reference("standard")
    assert ref.exists()
    assert ref.name == "standard.wav"


def test_resolve_reference_reuses_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mastering, "REFERENCE_DIR", tmp_path)
    existing = mastering.REFERENCE_DIR / "rock.wav"
    existing.write_bytes(b"\x00")  # fichier préexistant (contenu ignoré)
    ref = mastering._resolve_reference("rock")
    assert ref == existing
    assert ref.read_bytes() == b"\x00"  # jamais régénéré


def test_resolve_reference_unknown_preset_reuses_standard(tmp_path, monkeypatch):
    monkeypatch.setattr(mastering, "REFERENCE_DIR", tmp_path)
    ref = mastering._resolve_reference("jazz")
    assert ref.name == "standard.wav"
    assert ref.exists()


def test_resolve_reference_generation_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(mastering, "REFERENCE_DIR", tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("os error")

    monkeypatch.setattr(mastering, "_generate_reference_wav", _boom)
    with pytest.raises(mastering.ReferenceNotFoundError):
        mastering._resolve_reference("standard")


# --------------------------------------------------------------------------- #
# sum_stems — erreurs
# --------------------------------------------------------------------------- #
def test_sum_stems_rejects_empty_stems(tmp_path):
    with pytest.raises(mastering.NoStemError):
        mastering.sum_stems({}, {}, tmp_path / "mix.wav")


def test_sum_stems_rejects_missing_stem(tmp_path):
    with pytest.raises(mastering.StemNotFoundError):
        mastering.sum_stems({"vox": tmp_path / "absent.wav"}, {}, tmp_path / "mix.wav")


def test_sum_stems_rejects_sample_rate_mismatch(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    _write_stem(a, 44100, _noise_stereo(_SR, 0.2))
    _write_stem(b, 22050, _noise_stereo(22050, 0.2))
    with pytest.raises(mastering.SampleRateMismatchError):
        mastering.sum_stems({"a": a, "b": b}, {}, tmp_path / "mix.wav")


def test_sum_stems_rejects_non_finite_volume(tmp_path):
    stem = tmp_path / "vox.wav"
    _write_stem(stem, _SR, _noise_stereo(_SR, 0.2))
    with pytest.raises(mastering.InvalidVolumeError):
        mastering.sum_stems({"vox": stem}, {"vox": float("nan")}, tmp_path / "mix.wav")


@pytest.mark.parametrize("cutoff", [0.0, -1.0])
def test_sum_stems_rejects_non_positive_cutoff(tmp_path, cutoff):
    stem = tmp_path / "vox.wav"
    _write_stem(stem, _SR, _noise_stereo(_SR, 0.2))
    with pytest.raises(mastering.InvalidCutoffError):
        mastering.sum_stems({"vox": stem}, {}, tmp_path / "mix.wav",
                            highpass_cutoff=cutoff)


def test_sum_stems_rejects_cutoff_above_nyquist(tmp_path):
    stem = tmp_path / "vox.wav"
    _write_stem(stem, 8000, _noise_stereo(8000, 0.2))
    # Nyquist = 4000 Hz ; une coupure > 4000 doit être refusée.
    with pytest.raises(mastering.InvalidCutoffError):
        mastering.sum_stems({"vox": stem}, {}, tmp_path / "mix.wav",
                            highpass_cutoff=5000)


# --------------------------------------------------------------------------- #
# sum_stems — comportement nominal
# --------------------------------------------------------------------------- #
def test_sum_stems_writes_pcm16_stereo_of_max_length(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    _write_stem(a, _SR, _noise_stereo(_SR, 0.3))
    _write_stem(b, _SR, _noise_stereo(_SR, 0.6))
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"a": a, "b": b}, {}, out, highpass_cutoff=35.0)
    assert out.exists()
    data, sr = sf.read(str(out), dtype="float32")
    assert sr == _SR
    assert data.ndim == 2 and data.shape[1] == 2
    assert data.shape[0] == int(_SR * 0.6)  # longueur = max des stems
    assert float(np.max(np.abs(data))) <= 1.0


def test_sum_stems_volume_zero_silences_a_stem(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    _write_stem(a, _SR, _noise_stereo(_SR, 0.5, seed=7))
    _write_stem(b, _SR, _noise_stereo(_SR, 0.5, seed=8))
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"a": a, "b": b}, {"a": 0.0}, out, highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    # Seul « b » est mixé (a atténué à montage nul) : le bruit de b seul.
    ref, _ = sf.read(str(b), dtype="float32")
    rms = float(np.sqrt(np.mean(data ** 2)))
    ref_rms = float(np.sqrt(np.mean(ref ** 2)))
    # Le passe-haut coupe un peu d'énergie infra-basse mais pas 20 dB.
    assert 0.5 * ref_rms < rms < 1.5 * ref_rms


def test_sum_stems_duplicates_mono_to_stereo(tmp_path):
    # Un stem mono est dupliqué pour être sommé sur les deux canaux dès lors
    # qu'un stem stéréo impose un rendu 2 canaux. On mute le stem stéréo pour
    # n'observer que la contribution du stem mono.
    n = int(_SR * 0.2)
    rng = np.random.default_rng(3)
    mono = (rng.standard_normal(n) * 0.4).astype(np.float32)
    _write_stem(tmp_path / "mono.wav", _SR, mono)
    _write_stem(tmp_path / "stereo.wav", _SR, _noise_stereo(_SR, 0.3, seed=9))
    out = tmp_path / "mix.wav"
    mastering.sum_stems(
        {"mono": tmp_path / "mono.wav", "stereo": tmp_path / "stereo.wav"},
        {"mono": 1.0, "stereo": 0.0}, out, highpass_cutoff=35.0,
    )
    data, _ = sf.read(str(out), dtype="float32")
    assert data.ndim == 2 and data.shape[1] == 2
    np.testing.assert_allclose(data[:, 0], data[:, 1], atol=1e-4)


def test_sum_stems_normalizes_when_peak_exceeds_one(tmp_path):
    # Deux stems au bord de l'écrêtage, sommés, puis anti-écrêtage → pic <= 1.
    stem = tmp_path / "loud.wav"
    n = int(_SR * 0.2)
    rng = np.random.default_rng(4)
    sig = (0.7 * rng.standard_normal(n)).astype(np.float32)
    _write_stem(stem, _SR, np.stack([sig, sig], axis=1))
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"loud": stem}, {"loud": 2.0}, out, highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    assert float(np.max(np.abs(data))) <= 1.0


def test_sum_stems_highpass_removes_infrabass(tmp_path):
    # Signal à 5 Hz (sous la coupure) : il doit être fortement atténué.
    n = int(_SR * 1.0)
    t = np.arange(n) / _SR
    low = 0.8 * np.sin(2 * np.pi * 5.0 * t).astype(np.float32)
    _write_stem(tmp_path / "low.wav", _SR, np.stack([low, low], axis=1))
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"low": tmp_path / "low.wav"}, {}, out, highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    rms_in = float(np.sqrt(np.mean(np.stack([low, low], axis=1) ** 2)))
    rms_out = float(np.sqrt(np.mean(data ** 2)))
    assert rms_out < 0.1 * rms_in


# --------------------------------------------------------------------------- #
# apply_mastering
# --------------------------------------------------------------------------- #
def test_apply_mastering_missing_target_raises(tmp_path):
    with pytest.raises(mastering.StemNotFoundError):
        mastering.apply_mastering(tmp_path / "absent.wav", "standard",
                                  tmp_path / "out.wav")


def test_apply_mastering_wraps_matchering_errors(tmp_path, monkeypatch):
    import matchering

    def _boom(**kwargs):
        raise RuntimeError("matchering exploded")

    monkeypatch.setattr(matchering, "process", _boom)

    target = tmp_path / "mix.wav"
    _write_stem(target, _SR, _noise_stereo(_SR, 0.3))
    with pytest.raises(mastering.MasteringProcessError):
        mastering.apply_mastering(target, "standard", tmp_path / "out.wav")


def test_apply_mastering_uses_generated_reference(tmp_path, monkeypatch):
    # Répertoire de références vide : l'étalon de repli est généré à la volée.
    monkeypatch.setattr(mastering, "REFERENCE_DIR", tmp_path)
    target = tmp_path / "mix.wav"
    _write_stem(target, _SR, _noise_stereo(_SR, 0.3))
    out = tmp_path / "master.wav"
    mastering.apply_mastering(target, "standard", out)
    assert out.exists()
    # L'étalon de repli a bien été généré pour alimenter Matchering.
    assert (tmp_path / "standard.wav").exists()
    data, sr = sf.read(str(out), dtype="float32")
    assert sr == _SR and data.ndim == 2


# --------------------------------------------------------------------------- #
# Endpoints (via run_app)
# --------------------------------------------------------------------------- #
@pytest.fixture
def data(monkeypatch, tmp_path):
    """Isolation de ``DATA_DIR`` (comme le reste de la suite d'endpoints)."""
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    return tmp_path


def _make_track(data, track_id="abc123", with_stems=True, metabase=True):
    """Crée un morceau + ses stems, renvoie (dir, metadata dict)."""
    d = data / track_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {"id": track_id, "title": "Titre", "artist": "Artiste",
            "duration": 120.0, "status": "ready"}
    if metabase:
        (d / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if with_stems:
        stems = d / "stems"
        stems.mkdir(exist_ok=True)
        _write_stem(stems / "vocals.wav", _SR, _noise_stereo(_SR, 0.25, seed=11))
        _write_stem(stems / "drums.wav", _SR, _noise_stereo(_SR, 0.25, seed=12))
    return d, meta


def test_mastering_presets_endpoint():
    async def coro(client):
        r = await client.get("/api/mastering/presets")
        assert r.status_code == 200
        body = r.json()
        assert body["presets"] == ["standard", "rock", "acoustic"]

    run_app(main.app, coro)


def test_master_track_invalid_track_id(data):
    async def coro(client):
        # Identifiants refusés par ``_safe_track_id`` (préfixe « . ») ou hors
        # route : le mastering ne doit jamais être atteint.
        for bad in (".hidden", "nope"):
            r = await client.post(f"/api/tracks/{bad}/master", json={})
            assert r.status_code == 404, bad

    run_app(main.app, coro)


def test_master_track_no_stems_dir(data):
    d, _ = _make_track(data, "t1", with_stems=False)

    async def coro(client):
        r = await client.post("/api/tracks/t1/master", json={})
        assert r.status_code == 404
        assert "stem" in r.json()["detail"].lower()

    run_app(main.app, coro)


def test_master_track_only_mix_stem_is_unusable(data):
    d, _ = _make_track(data, "t2", with_stems=False)
    stems = d / "stems"
    stems.mkdir(exist_ok=True)
    _write_stem(stems / "mix.wav", _SR, _noise_stereo(_SR, 0.5))

    async def coro(client):
        r = await client.post("/api/tracks/t2/master", json={})
        assert r.status_code == 404
        assert "aucun stem utilisable" in r.json()["detail"].lower()

    run_app(main.app, coro)


def test_master_track_rejects_invalid_volume(data):
    d, _ = _make_track(data, "t3")

    async def coro(client):
        # 1e400 est un nombre JSON valide que json.loads convertit en +inf :
        # httpx refuse d'encoder un inf dans ``json=``, on envoie donc le corps
        # brut ; côté serveur ``request.json()`` le parse, puis ``_validate_volume``
        # le rejette → MasteringError → HTTP 400.
        r = await client.post(
            "/api/tracks/t3/master",
            content='{"volumes": {"vocals": 1e400}, "preset": "standard"}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    run_app(main.app, coro)


def test_master_track_success_writes_master_and_metadata(data):
    d, _ = _make_track(data, "t4")

    async def coro(client):
        r = await client.post(
            "/api/tracks/t4/master",
            json={"volumes": {"vocals": 1.0, "drums": 0.8}, "preset": "rock"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["master_url"] == "/data/t4/master.wav"
        assert (d / "master.wav").exists()
        meta = json.loads((d / "metadata.json").read_text("utf-8"))
        assert meta["files"]["master"] == "/data/t4/master.wav"

    run_app(main.app, coro)


def test_master_track_unknown_preset_falls_back(data):
    d, _ = _make_track(data, "t5")

    async def coro(client):
        # Libellé de preset inconnu → repli sur « standard » et succès quand même.
        r = await client.post(
            "/api/tracks/t5/master",
            json={"volumes": {}, "preset": "metal"},
        )
        assert r.status_code == 200, r.text
        assert (d / "master.wav").exists()

    run_app(main.app, coro)


# --------------------------------------------------------------------------- #
# Non-régression : borne de magnitude du gain, mapping 400/500, génération de
# référence avec parent absent, absence de résidu de mix intermédiaire.
# --------------------------------------------------------------------------- #
def test_sum_stems_rejects_volume_magnitude_above_max_gain(tmp_path):
    """Un gain fini mais dont |g| dépasse ``MAX_GAIN`` est refusé (anti-overflow)."""
    stem = tmp_path / "vox.wav"
    _write_stem(stem, _SR, _noise_stereo(_SR, 0.2))
    excessive = mastering.MAX_GAIN * 2
    with pytest.raises(mastering.InvalidVolumeError):
        mastering.sum_stems({"vox": stem}, {"vox": excessive}, tmp_path / "mix.wav")


def test_sum_stems_accepts_max_gain_without_overflow(tmp_path):
    """Un gain égal à ``MAX_GAIN`` reste accepté et le master reste sain (pic <= 1)."""
    stem = tmp_path / "vox.wav"
    _write_stem(stem, _SR, _noise_stereo(_SR, 0.01, seed=21))
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"vox": stem}, {"vox": mastering.MAX_GAIN}, out,
                        highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    # Sommation float64 + clamp : aucune valeur NaN/Inf ni hors de [-1, 1].
    assert np.isfinite(data).all()
    assert float(np.max(np.abs(data))) <= 1.0
    assert float(np.max(np.abs(data))) > 0.0  # signal présent, non décimé à zéro


def test_generate_reference_wav_creates_missing_parent(tmp_path):
    """La génération d'un étalon crée le parent manquant (ex. arborescence profonde)."""
    out = tmp_path / "deeply" / "nested" / "refs" / "standard.wav"
    mastering._generate_reference_wav(out, "standard", sample_rate=22050, duration=0.2)
    assert out.exists()
    data, sr = sf.read(str(out), dtype="float32")
    assert sr == 22050 and data.ndim == 2


def test_master_track_rejects_excessive_volume_400(data):
    """Un volume fini mais hors bornes (|g| > MAX_GAIN) → 400 (erreur client)."""
    d, _ = _make_track(data, "t6")

    async def coro(client):
        r = await client.post(
            "/api/tracks/t6/master",
            json={"volumes": {"vocals": 1e6}, "preset": "standard"},
        )
        assert r.status_code == 400
        assert "hors bornes" in r.json()["detail"].lower()

    run_app(main.app, coro)


def test_master_track_mastering_process_error_500(data, monkeypatch):
    """``MasteringProcessError`` (échec Matchering) → 500, jamais 400."""
    d, _ = _make_track(data, "t7")

    def _boom(target_wav, reference_preset, output_wav):
        raise mastering.MasteringProcessError("matchering crashed")

    monkeypatch.setattr(main, "apply_mastering", _boom)

    async def coro(client):
        r = await client.post("/api/tracks/t7/master", json={})
        assert r.status_code == 500
        assert "interne" in r.json()["detail"].lower()

    run_app(main.app, coro)


def test_master_track_reference_not_found_500(data, monkeypatch):
    """``ReferenceNotFoundError`` (référence indisponible/génération en échec) → 500."""
    d, _ = _make_track(data, "t8")

    def _boom(target_wav, reference_preset, output_wav):
        raise mastering.ReferenceNotFoundError("no reference available")

    monkeypatch.setattr(main, "apply_mastering", _boom)

    async def coro(client):
        r = await client.post("/api/tracks/t8/master", json={})
        assert r.status_code == 500
        assert "interne" in r.json()["detail"].lower()

    run_app(main.app, coro)


def test_master_track_cleans_tmp_mix_dir_and_no_residue(data, monkeypatch):
    """Le mix intermédiaire vit dans un répertoire temporaire, purgé en finally :
    aucun répertoire temporaire ni résidu ``*_master_mix*`` ne subsiste sous
    ``data/`` après un mastering réussi."""
    d, _ = _make_track(data, "t9")
    tmp_mix = data / "master_tmp"
    monkeypatch.setattr(main.tempfile, "mkdtemp", lambda prefix=None: str(tmp_mix))

    async def coro(client):
        r = await client.post("/api/tracks/t9/master", json={})
        assert r.status_code == 200, r.text
        # Le répertoire temporaire du mix a été supprimé en finally.
        assert not tmp_mix.exists()
        # Aucun résidu de mix intermédiaire dans le dossier de données.
        residues = [p for p in data.rglob("*master_mix*")]
        assert residues == []

    run_app(main.app, coro)


def test_master_track_sample_rate_mismatch_500(data):
    """Échantillonnage incohérent entre stems : erreur technique → 500 (jamais 400)."""
    d, _ = _make_track(data, "t10", with_stems=False)
    stems = d / "stems"
    stems.mkdir(exist_ok=True)
    _write_stem(stems / "a.wav", 44100, _noise_stereo(44100, 0.2))
    _write_stem(stems / "b.wav", 22050, _noise_stereo(22050, 0.2))

    async def coro(client):
        r = await client.post("/api/tracks/t10/master", json={})
        assert r.status_code == 500
        assert "interne" in r.json()["detail"].lower()

    run_app(main.app, coro)


def test_master_track_cleans_tmp_mix_dir_even_on_error(data, monkeypatch):
    """Même en cas d'échec (MasteringError → 500), le répertoire temporaire du
    mix est purgé par le ``finally`` de l'endpoint."""
    d, _ = _make_track(data, "t11")
    tmp_mix = data / "master_tmp_fail"
    monkeypatch.setattr(main.tempfile, "mkdtemp", lambda prefix=None: str(tmp_mix))

    def _boom(target_wav, reference_preset, output_wav):
        raise mastering.ReferenceNotFoundError("no reference available")

    monkeypatch.setattr(main, "apply_mastering", _boom)

    async def coro(client):
        r = await client.post("/api/tracks/t11/master", json={})
        assert r.status_code == 500
        assert not tmp_mix.exists()

    run_app(main.app, coro)


# --------------------------------------------------------------------------- #
# _sanitize_finite (assainissement des échantillons non finis)
# --------------------------------------------------------------------------- #
def test_sanitize_finite_returns_input_unchanged_when_finite():
    data = np.array([[0.1, -0.2], [0.3, 0.4]], dtype=np.float32)
    out = mastering._sanitize_finite(data, "vox")
    assert out is data          # aucune copie inutile si déjà sain
    np.testing.assert_array_equal(out, data)


def test_sanitize_finite_replaces_non_finite_with_silence():
    data = np.array([[0.1, np.nan], [np.inf, -np.inf]], dtype=np.float32)
    out = mastering._sanitize_finite(data, "vox")
    assert out is not data               # copie (l'entrée n'est pas mutée)
    assert np.isfinite(out).all()
    assert out[1, 0] == 0.0 and out[1, 1] == 0.0 and out[0, 1] == 0.0
    # Les échantillons sains sont conservés (comparaison en float32).
    assert out[0, 0] == np.float32(0.1)
    # L'entrée conserve ses NaN/inf (aucun effet de bord).
    assert np.isnan(data[0, 1]) and not np.isfinite(data[1, 0])


def test_sum_stems_all_nan_stem_yields_silent_finite_mix(tmp_path):
    """Un stem intégralement NaN devient silencieux : le master reste fini (≠
    NoStemError, car le stem possède des trames) et n'est pas corrompu.
    Le fichier est écrit en FLOAT pour préserver réellement les NaN (un WAV
    PCM_16 les quantifierait en valeur finie et ne testerait rien)."""
    n = int(_SR * 0.3)
    bad = np.full((n, 2), np.nan, dtype=np.float32)
    _write_stem(tmp_path / "bad.wav", _SR, bad, subtype="FLOAT")
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"bad": tmp_path / "bad.wav"}, {}, out, highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    assert np.isfinite(data).all()
    assert float(np.max(np.abs(data))) == 0.0


# --------------------------------------------------------------------------- #
# Non-régression : stems vides et échantillons non finis
# --------------------------------------------------------------------------- #
def test_sum_stems_rejects_all_empty_stems(tmp_path):
    """Tous les stems sont vides (0 trame) → NoStemError explicite, pas de WAV nul."""
    _write_stem(tmp_path / "a.wav", _SR, np.zeros((0, 2), dtype=np.float32))
    _write_stem(tmp_path / "b.wav", _SR, np.zeros((0, 2), dtype=np.float32))
    with pytest.raises(mastering.NoStemError) as exc:
        mastering.sum_stems({"a": tmp_path / "a.wav", "b": tmp_path / "b.wav"},
                            {}, tmp_path / "mix.wav")
    assert "vide" in str(exc.value).lower()


def test_sum_stems_ignores_one_empty_stem_if_others_have_frames(tmp_path):
    """Un stem vide parmi des stems chargés ne bloque pas la sommation."""
    _write_stem(tmp_path / "empty.wav", _SR, np.zeros((0, 2), dtype=np.float32))
    _write_stem(tmp_path / "voice.wav", _SR, _noise_stereo(_SR, 0.3, seed=5))
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"empty": tmp_path / "empty.wav",
                         "voice": tmp_path / "voice.wav"},
                        {}, out, highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    assert float(np.max(np.abs(data))) > 0.0


def test_sum_stems_sanitizes_non_finite_samples(tmp_path):
    """Un stem contenant NaN/±inf produit un master sain (aucun non-fini, pic <= 1).

    On écrit le fichier en FLOAT pour préserver réellement les NaN/±inf : un
    WAV PCM_16 les quantifierait en valeurs finies et la sanitisation ne serait
    jamais exercée."""
    n = int(_SR * 0.3)
    t = np.arange(n) / _SR
    sig = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sig[10] = np.nan
    sig[20] = np.inf
    sig[30] = -np.inf
    _write_stem(tmp_path / "bad.wav", _SR, np.stack([sig, sig], axis=1),
                subtype="FLOAT")
    out = tmp_path / "mix.wav"
    mastering.sum_stems({"bad": tmp_path / "bad.wav"}, {}, out, highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    assert bool(np.all(np.isfinite(data)))
    assert float(np.max(np.abs(data))) <= 1.0


def test_sum_stems_non_finite_does_not_affect_other_stem(tmp_path):
    """Les échantillons non finis d'un stem sont neutralisés ; un stem sain voisin
    reste présent dans le master final."""
    n = int(_SR * 0.3)
    rng = np.random.default_rng(6)
    clean = (0.25 * rng.standard_normal(n)).astype(np.float32)
    _write_stem(tmp_path / "clean.wav", _SR, np.stack([clean, clean], axis=1))

    bad = np.stack([np.zeros(n, dtype=np.float32)] * 2, axis=1)
    bad[0, 0] = np.nan
    # FLOAT : préserve le NaN pour exercer réellement la sanitisation.
    _write_stem(tmp_path / "bad.wav", _SR, bad, subtype="FLOAT")

    out = tmp_path / "mix.wav"
    mastering.sum_stems({"clean": tmp_path / "clean.wav",
                         "bad": tmp_path / "bad.wav"},
                        {}, out, highpass_cutoff=35.0)
    data, _ = sf.read(str(out), dtype="float32")
    assert bool(np.all(np.isfinite(data)))
    rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
    # Le stem sain contribue : le master n'est pas muet.
    assert rms > 0.0


def test_master_track_all_empty_stems_returns_500(data):
    """Via l'endpoint : stems tous vides → NoStemError → 500 explicite."""
    d, _ = _make_track(data, "t12", with_stems=False)
    stems = d / "stems"
    stems.mkdir(exist_ok=True)
    _write_stem(stems / "a.wav", _SR, np.zeros((0, 2), dtype=np.float32))
    _write_stem(stems / "b.wav", _SR, np.zeros((0, 2), dtype=np.float32))

    async def coro(client):
        r = await client.post("/api/tracks/t12/master", json={})
        assert r.status_code == 500
        assert "interne" in r.json()["detail"].lower()

    run_app(main.app, coro)
