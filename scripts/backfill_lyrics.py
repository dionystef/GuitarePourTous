#!/usr/bin/env python3
"""Rattrapage par lot des paroles via resolve_lyrics (LRCLIB puis Whisper).

Sans option, les morceaux disposant déjà d'un lyrics.json non vide sont sautés.
Avec ``--force``, chaque morceau est ré-évalué et un éventuel lyrics.json Whisper
est écrasé par les paroles officielles LRCLIB si elles sont disponibles.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from services.lyrics_provider import resolve_lyrics

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("guitarlab.backfill")

DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))


def _load_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text("utf-8"))
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Rattrapage des paroles (LRCLIB / Whisper).")
    parser.add_argument("--force", action="store_true",
                        help="Ré-évaluer chaque morceau (écrase les lyrics.json existants).")
    args = parser.parse_args()

    tracks = [p for p in DATA_DIR.iterdir() if p.is_dir() and (p / "metadata.json").exists()]
    print(f"--- Rattrapage Paroles ({len(tracks)} morceaux dans {DATA_DIR}) [force={args.force}] ---")
    if not tracks:
        return

    done = 0
    official = 0
    none = 0
    for t_dir in sorted(tracks):
        lyrics_path = t_dir / "lyrics.json"
        if lyrics_path.exists() and lyrics_path.stat().st_size > 0 and not args.force:
            print(f"⏩ [SKIP] {t_dir.name} (déjà présent) — utilise --force pour ré-évaluer")
            continue

        vocals_path = t_dir / "stems" / "vocals.mp3"
        meta = _load_meta(t_dir / "metadata.json")
        title = meta.get("title", "") or t_dir.name
        artist = meta.get("artist", "")
        duration = meta.get("duration", 0)

        print(f"🎤 [RESOLVE] {t_dir.name}...", flush=True)
        try:
            lyrics, src = resolve_lyrics(title, artist, duration, vocals_path, lyrics_path)
            if src == "lrclib":
                print(f"   ✔ [lrclib] {len(lyrics)} phrases (officielles, < 500 ms)", flush=True)
                official += 1
            elif src == "whisper":
                print(f"   ✔ [whisper] {len(lyrics)} phrases (IA)", flush=True)
            else:
                print(f"   ℹ [none] aucune parole", flush=True)
                none += 1
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Échec %s : %s", t_dir.name, e)
            print(f"   ✖ Erreur : {e}", flush=True)

    print(f"--- Terminé : {done} traitées ({official} officielles LRCLIB, {none} aucune) ---")


if __name__ == "__main__":
    main()
