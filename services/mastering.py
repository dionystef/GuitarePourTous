"""Mastering audio : sommation pondérée des stems + calibrage Matchering.

Ce service automatise la production d'un master à partir des stems séparés
(Demucs) et des balances de volume choisies par l'utilisateur :

  1. ``sum_stems`` — somme les stems pondérés par leurs volumes, aligne les
     longueurs, applique un passe-haut Butterworth (ordre 2) pour éliminer les
     infrabasses de smartphone, puis écrit le mix intermédiaire en WAV 16-bit ;
  2. ``apply_mastering`` — calibre ce mix contre un fichier de référence
     (preset ``standard``/``rock``/``acoustic``) via Matchering ;
  3. ``get_presets`` — expose la liste ordonnée des presets disponibles.

Les fichiers de référence vivent dans ``references/<preset>.wav`` à la racine
du dépôt. S'ils sont absents, un étalon de repli (spectre harmonique stéréo
déterministe) est généré à la volée afin que le mastering ne reste jamais
bloqué par un preset manquant.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Final

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

logger = logging.getLogger("guitarlab.mastering")

# --------------------------------------------------------------------------- #
# Presets / répertoire des références
# --------------------------------------------------------------------------- #
DEFAULT_PRESET: Final[str] = "standard"
REFERENCE_PRESETS: Final[list[str]] = ["standard", "rock", "acoustic"]
REFERENCE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "references"
_DEFAULT_SR: Final[int] = 44100
# Borne de magnitude du gain (gain multiplicatif) : au-delà, la valeur est
# rejetée comme entrée invalide. Évite qu'un gain absurde n'explose le mix en
# float32 et ne corrompe silencieusement le master.
MAX_GAIN: Final[float] = 100.0

REFERENCE_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Exceptions dédiées (gestion d'erreurs granulaire)
# --------------------------------------------------------------------------- #
class MasteringError(Exception):
    """Erreur de base du moteur de mastering (toute erreur métier en dérive)."""


class NoStemError(MasteringError):
    """Aucun stem exploitable n'a été fourni pour la sommation."""


class StemNotFoundError(MasteringError):
    """Un stem requis est introuvable sur disque."""


class SampleRateMismatchError(MasteringError):
    """Les stems ne partagent pas la même fréquence d'échantillonnage."""


class InvalidVolumeError(MasteringError):
    """Une balance de volume est invalide (non finie ou absurde)."""


class InvalidCutoffError(MasteringError):
    """La fréquence de coupure du passe-haut est hors des bornes valides."""


class ReferenceNotFoundError(MasteringError):
    """Impossible de déterminer un fichier de référence exploitable."""


class MasteringProcessError(MasteringError):
    """Matchering a échoué pendant l'application du mastering."""


# --------------------------------------------------------------------------- #
# Fonctions publiques
# --------------------------------------------------------------------------- #
def get_presets() -> list[str]:
    """Retourne la liste ordonnée des presets de référence disponibles.

    Returns:
        list[str]: Les noms de presets (``["standard", "rock", "acoustic"]``).
    """
    return list(REFERENCE_PRESETS)


def _normalize_preset(preset: str) -> str:
    """Ramène un libellé de preset sur un nom connu, avec repli sur ``standard``.

    Paramètres:
        preset: libellé envoyé par l'appelant (insensible à la casse/espaces).

    Returns:
        str: nom de preset normalisé, jamais hors de ``REFERENCE_PRESETS``.
    """
    p = str(preset or "").strip().lower()
    if p not in REFERENCE_PRESETS:
        logger.warning("Preset de référence inconnu « %s » → repli sur « %s »",
                       preset, DEFAULT_PRESET)
        return DEFAULT_PRESET
    return p


def _validate_volume(name: str, value: float) -> float:
    """Valide et normalise une balance de volume en gain multiplicatif.

    Rejette les valeurs non finies (NaN/inf) — un gain infini écraserait le mix —
    et les valeurs dont la magnitude dépasse ``MAX_GAIN``, afin d'éviter un
    débordement float32 et la corruption silencieuse du master.

    Paramètres:
        name: nom du stem (pour le message d'erreur).
        value: gain demandé.

    Returns:
        float: gain normalisé.

    Raises:
        InvalidVolumeError: si la valeur n'est pas finie ou dépasse ``MAX_GAIN``.
    """
    v = float(value)
    if not math.isfinite(v):
        raise InvalidVolumeError(f"Volume invalide pour le stem « {name} » : {value!r}")
    if abs(v) > MAX_GAIN:
        raise InvalidVolumeError(
            f"Volume hors bornes pour le stem « {name} » : {value!r} "
            f"(|gain| ≤ {MAX_GAIN:g}).")
    return v


def _generate_reference_wav(path: Path, preset: str,
                            sample_rate: int = _DEFAULT_SR,
                            duration: float = 4.0) -> Path:
    """Génère un étalon de repli déterministe quand le fichier de référence manque.

    Le spectre repose sur une somme d'harmoniques (décroissance ~1/f) posée sur
    une fréquence fondamentale propre au preset, ce qui fournit à Matchering une
    cible exploitable même sans étalon commercial fourni :
      * ``standard`` — équilibre neutre ;
      * ``rock``  — fondamental plus aigu, harmoniques plus préservées ;
      * ``acoustic`` — fondamental plus grave, harmoniques plus amorties.

    L'écriture est stéréo, normalisée (pic ~0.6) et reproductible (graine fixe).

    Paramètres:
        path: chemin du fichier WAV à écrire.
        preset: preset ciblé (influence le gabarit spectral).
        sample_rate: fréquence d'échantillonnage (Hz).
        duration: durée de l'étalon (s).

    Returns:
        Path: ``path`` rempli, prêt à servir de référence.
    """
    n = int(sample_rate * duration)
    t = np.arange(n, dtype=np.float64) / sample_rate
    rng = np.random.default_rng(42)

    # Gabarit spectral par preset : fondamental + raideur de la queue harmonique.
    fundamental = {"standard": 210.0, "rock": 233.0, "acoustic": 196.0}.get(preset, 210.0)
    rolloff = {"standard": 0.8, "rock": 0.55, "acoustic": 0.95}.get(preset, 0.8)

    tonal = np.zeros(n, dtype=np.float64)
    mult = 1
    while True:
        freq = fundamental * mult
        if freq >= sample_rate * 0.45:  # garde-fou vis-à-vis de Nyquist
            break
        amp = 1.0 / (mult ** rolloff)
        # « Rock » : léger renfort des harmoniques médium (2..6).
        if preset == "rock" and 2 <= mult <= 6:
            amp *= 1.25
        tonal += amp * np.sin(2.0 * math.pi * freq * t + rng.uniform(0.0, 2.0 * math.pi))
        mult += 1

    # Tapis de bruit léger : évite un spectre purement tonal, plus proche d'un
    # matériel réel, tout en restant déterministe.
    left = tonal + 0.03 * rng.standard_normal(n)
    right = tonal + 0.03 * rng.standard_normal(n)
    stereo = np.stack([left, right], axis=1)

    peak = float(np.max(np.abs(stereo))) or 1e-9
    stereo = (stereo / peak) * 0.6
    # Le parent peut être absent (ex. tests sur un dossier temporaire) : on le
    # crée systématiquement avant d'écrire, comme pour toute sortie WAV.
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), stereo.astype(np.float32), sample_rate, subtype="PCM_16")
    logger.info("Référence générée : %s (preset « %s », %.1f s)",
                path.name, preset, duration)
    return path


def _resolve_reference(preset: str) -> Path:
    """Résout un fichier de référence WAV pour un preset, avec repli sûr.

    Si le fichier ``references/<preset>.wav`` est absent, un étalon de repli est
    généré. Ce mécanisme garantit que ``apply_mastering`` ne se retrouve jamais
    sans cible de référence (le repli sur preset manquant demandé par la
    mission).

    Paramètres:
        preset: libellé de preset ; un nom inconnu retombe sur ``standard``.

    Returns:
        Path: chemin vers un fichier de référence exploitable et existant.

    Raises:
        ReferenceNotFoundError: si ni le fichier ni sa génération ne réussissent.
    """
    # Normalisation défensive : un libellé inconnu (ou déjà normalisé) retombe
    # systématiquement sur le preset par défaut.
    preset = _normalize_preset(preset)

    path = REFERENCE_DIR / f"{preset}.wav"
    if not path.exists():
        logger.warning("Référence « %s » absente → génération d'un étalon de repli",
                       path.name)
        try:
            _generate_reference_wav(path, preset)
        except Exception as exc:  # noqa: BLE001 — repli de dernier recours
            logger.error("Génération de la référence « %s » en échec", path.name)
            raise ReferenceNotFoundError(
                f"Référence « {preset} » indisponible et non générable.") from exc
    return path


def _sanitize_finite(data: np.ndarray, name: str) -> np.ndarray:
    """Remplace les échantillons non finis (NaN/±inf) d'un stem par du silence.

    Un fichier audio corrompu ou un décodeur défaillant peut produire des
    ``NaN`` qui, une fois sommés, propageraient la valeur à tout le master.
    On les remplace donc par ``0.0`` (silence) et on journalise le nombre de
    trames corrigées, sans jamais faire échouer l'opération.

    Paramètres:
        data: signal lu (float32, ``(frames, channels)``).
        name: nom du stem, pour le message de journalisation.

    Returns:
        np.ndarray: ``data`` dont tous les échantillons sont finis (copie si
            des valeurs ont été corrigées, sinon l'entrée inchangée).
    """
    finite = np.isfinite(data)
    if not np.all(finite):
        count = int(np.count_nonzero(~finite))
        logger.warning(
            "Stem « %s » contient %d échantillon(s) non fini(s) "
            "→ remplacés par du silence.", name, count)
        data = np.where(finite, data, np.float32(0.0)).astype(np.float32)
    return data


def _pad_to(data: np.ndarray, length: int, channels: int) -> np.ndarray:
    """Allonge et ajuste un stem (frames, ch) à ``(length, channels)``.

    Un stem mono (1 canal) est dupliqué pour être sommé sur les deux canaux ;
    un stem plus court est complété par des zéros ; un excès de canaux est
    tronqué. Toute opération est sans mutation de l'entrée.

    Paramètres:
        data: tableau (frames, ch) à aligner.
        length: nombre de trames cible.
        channels: nombre de canaux cible.

    Returns:
        np.ndarray: tableau aligné ``(length, channels)``.
    """
    # Mise au même nombre de canaux (duplication mono → stéréo).
    if data.shape[1] == 1:
        data = np.repeat(data, channels, axis=1)
    elif data.shape[1] > channels:
        data = data[:, :channels]
    elif data.shape[1] < channels:
        pad = np.zeros((data.shape[0], channels - data.shape[1]), dtype=data.dtype)
        data = np.concatenate([data, pad], axis=1)
    # Rallongement des trames manquantes (alignement des tailles).
    if data.shape[0] < length:
        pad = np.zeros((length - data.shape[0], channels), dtype=data.dtype)
        data = np.concatenate([data, pad], axis=0)
    return data


def sum_stems(stem_paths: dict[str, Path], stem_volumes: dict[str, float],
              output_wav: Path, highpass_cutoff: float = 35.0) -> Path:
    """Somme les stems pondérés et écrit le mix intermédiaire en WAV.

    Étapes :
      1. lecture de chaque stem via ``soundfile.read`` (float32, stéréo) ;
      2. assainissement des échantillons non finis (NaN/±inf → silence) ;
      3. application du gain multiplicatif (repli à ``1.0`` si le volume est
         omis) ;
      4. alignement des longueurs puis sommation des signaux (accumulateur
         float64) ;
      5. filtre passe-haut Butterworth (ordre 2, ~35 Hz) anti-infrabasses ;
      6. clamp final dans [-1, 1] puis écriture du mix en WAV 16-bit.

    Paramètres:
        stem_paths: mapping ``nom_de_stem -> chemin`` des stems à mixer.
        stem_volumes: mapping ``nom_de_stem -> gain`` ; un stem absent de ce
            dict est mixé à l'unité.
        output_wav: chemin du fichier WAV produit (intermédiaire).
        highpass_cutoff: fréquence de coupure du passe-haut (Hz).

    Returns:
        Path: ``output_wav`` écrit.

    Raises:
        NoStemError: aucun stem fourni ou tous les stems sont vides.
        StemNotFoundError: un fichier de stem est absent.
        SampleRateMismatchError: fréquences d'échantillonnage hétérogènes.
        InvalidVolumeError: volume non fini.
        InvalidCutoffError: coupure hors des bornes physiques.
    """
    if not stem_paths:
        raise NoStemError("Aucun stem fourni pour la sommation.")

    sample_rate: int | None = None
    target_length = 0
    target_channels = 1
    loaded: dict[str, np.ndarray] = {}

    for name, path in stem_paths.items():
        if not path.exists():
            raise StemNotFoundError(f"Stem « {name} » introuvable : {path}")
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        # Assainissement : un échantillon NaN/±inf (fichier corrompu, décodeur
        # MP3 défaillant) ne doit jamais se propager au master. On le remplace
        # par du silence (0) et on journalise, plutôt que de corrompre le rendu
        # final ou de faire échouer toute l'opération.
        data = _sanitize_finite(data, name)
        if sample_rate is None:
            sample_rate = int(sr)
        elif int(sr) != sample_rate:
            raise SampleRateMismatchError(
                f"Fréquence d'échantillonnage incohérente pour « {name} » "
                f"({sr} Hz vs {sample_rate} Hz).")
        # Gain multiplicatif : repli à 1.0 si le volume est omis.
        gain = _validate_volume(name, stem_volumes.get(name, 1.0))
        data = data * np.float32(gain)
        loaded[name] = data
        target_length = max(target_length, data.shape[0])
        target_channels = max(target_channels, data.shape[1])

    if sample_rate is None:
        raise NoStemError("Aucun stem exploitable pour la sommation.")
    # Tous les stems ne contiennent aucune trame (fichiers vides) : impossible
    # de produire un mix, on refuse avec un message clair plutôt que d'écrire
    # un WAV vide.
    if target_length == 0:
        raise NoStemError(
            "Tous les stems sont vides (aucune trame audio lisible).")

    # Bornes physiques du filtre : la coupure doit rester strictement sous
    # Nyquist (sinon `butter` lèverait une erreur peu lisible).
    nyquist = sample_rate / 2.0
    if not (0.0 < highpass_cutoff < nyquist):
        raise InvalidCutoffError(
            f"Fréquence de coupure invalide ({highpass_cutoff} Hz) : "
            f"doit être dans ]0, {nyquist:.1f} Hz[.")

    # Sommation en float64 : évite toute accumulation de précision/overflow
    # float32 (déjà bornée par `MAX_GAIN`, mais par sécurité) avant le clamp.
    mixed = np.zeros((target_length, target_channels), dtype=np.float64)
    for data in loaded.values():
        mixed += _pad_to(data, target_length, target_channels)

    # Filtre passe-haut anti-infrabasses : appliqué sur l'axe temporel (0) pour
    # que chaque canal soit traité indépendamment. `output="sos"` offre une
    # stabilité numérique supérieure à la forme b-a.
    sos = butter(2, highpass_cutoff, btype="highpass", fs=float(sample_rate), output="sos")
    filtered = sosfilt(sos, mixed, axis=0)

    # Anti-écrêtage / clamp : on ne réduit le niveau que si la somme dépasse
    # l'unité, ce qui préserve les balances choisies par l'utilisateur. La
    # conversion finale en float32 est bornée (aucune valeur hors de [-1, 1]).
    peak = float(np.max(np.abs(filtered))) if filtered.size else 0.0
    if peak > 1.0:
        filtered = filtered / peak
    out = np.clip(filtered, -1.0, 1.0).astype(np.float32)

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_wav), out, sample_rate, subtype="PCM_16")
    logger.info("Mix intermédiaire écrit : %s (%d trams, %d canaux, %.0f Hz)",
                output_wav, target_length, target_channels, sample_rate)
    return output_wav


def apply_mastering(target_wav: Path, reference_preset: str, output_wav: Path) -> Path:
    """Calibre un mix intermédiaire contre une référence via Matchering.

    Paramètres:
        target_wav: mix intermédiaire (produit par ``sum_stems``) à masteriser.
        reference_preset: preset de référence (``standard`` par défaut si le
            libellé est inconnu).
        output_wav: chemin du master final produit (WAV 16-bit).

    Returns:
        Path: ``output_wav`` écrit.

    Raises:
        StemNotFoundError: ``target_wav`` est introuvable.
        ReferenceNotFoundError: aucune référence n'a pu être résolue/générée.
        MasteringProcessError: Matchering a échoué.
    """
    if not target_wav.exists():
        raise StemNotFoundError(f"Fichier cible introuvable : {target_wav}")

    preset = _normalize_preset(reference_preset)
    ref_wav = _resolve_reference(preset)

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Import différé : le cœur du mastering n'est requis que lors de
        # l'application, laissant `sum_stems`/`get_presets` utilisables seuls.
        import matchering as mg
        mg.process(
            target=str(target_wav),
            reference=str(ref_wav),
            results=[mg.pcm16(str(output_wav))],
        )
    except Exception as exc:  # noqa: BLE001 — erreur technique détaillée ici
        logger.exception("Matchering en échec sur %s (réf %s)", target_wav, ref_wav)
        raise MasteringProcessError(f"Matchering a échoué : {exc}") from exc

    logger.info("Master produit : %s (preset « %s »)", output_wav, preset)
    return output_wav
