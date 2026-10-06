"""Transcription des paroles via faster-whisper sur la piste vocale isolée."""
from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("guitarlab.lyrics")

_default_models = Path.home() / ".local" / "share" / "guitarlab" / "models"
MODELS_DIR = Path(os.environ.get("MODELS_DIR", _default_models))

# Artefacts fréquents produits par whisper (habillage des sous-titres, crédits,
# mentions de services de transcription) : on les écarte pour ne pas polluer les
# paroles affichées. Insensible à la casse.
_HALLUCINATION_PATTERNS = [
    re.compile(r"amara\.org", re.IGNORECASE),
    re.compile(r"sous-titres réalisés par", re.IGNORECASE),
    re.compile(r"transcription réalisée par", re.IGNORECASE),
    re.compile(r"sous-titrage", re.IGNORECASE),
]


def _is_hallucinated(text: str) -> bool:
    """Vrai si le segment correspond à un motif anti-hallucination connu."""
    return any(p.search(text) for p in _HALLUCINATION_PATTERNS)


def is_model_cached(model_size: str = "small") -> bool:
    """Vérifie si les poids du modèle sont déjà présents en cache local."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    matches = list(MODELS_DIR.glob(f"*{model_size}*"))
    return len(matches) > 0


def transcribe_vocals(
    vocals_path: str | Path,
    output_json_path: str | Path,
    model_size: str = "small",
) -> list[dict]:
    """Transcrit la piste vocale isolée et écrit les segments [{start, end, text}].

    Résolution matérielle dynamique : CUDA/float16 si dispo, sinon CPU/int8,
    avec repli propre sur CPU en cas d'échec du chargement GPU.
    """
    from faster_whisper import WhisperModel
    import torch

    vocals_path = Path(vocals_path)
    if not vocals_path.exists():
        raise FileNotFoundError(f"Piste vocale introuvable : {vocals_path}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    cuda_ok = torch.cuda.is_available() if hasattr(torch, "cuda") else False
    if cuda_ok:
        dev, comp = "cuda", "float16"
    else:
        dev, comp = "cpu", "int8"

    logger.info("faster-whisper (%s) sur %s (%s), cache=%s", model_size, dev, comp, MODELS_DIR)
    try:
        model = WhisperModel(model_size, device=dev, compute_type=comp, download_root=str(MODELS_DIR))
    except Exception as exc:
        if dev == "cuda":
            logger.warning("Échec CUDA (%s), repli CPU int8...", exc)
            dev, comp = "cpu", "int8"
            model = WhisperModel(model_size, device="cpu", compute_type="int8", download_root=str(MODELS_DIR))
        else:
            raise

    segments_iter, info = model.transcribe(
        str(vocals_path),
        beam_size=5,
        vad_filter=True,
    )

    lyrics = [
        {
            "start": round(float(seg.start), 2),
            "end": round(float(seg.end), 2),
            "text": seg.text.strip(),
        }
        for seg in segments_iter
        if seg.text.strip() and not _is_hallucinated(seg.text)
    ]

    logger.info("✔ %d segments paroles extraits (langue: %s)", len(lyrics), info.language)

    out = Path(output_json_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lyrics, ensure_ascii=False, indent=2), encoding="utf-8")

    return lyrics
