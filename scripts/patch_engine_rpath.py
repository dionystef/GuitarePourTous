#!/usr/bin/env python3
"""Prépare le moteur PyInstaller pour le bundling AppImage (linuxdeploy).

Contexte : le moteur ``guitarlab-engine`` (PyInstaller ``--onedir``) regroupe
ses dépendances natives dans des sous-dossiers (``torch/lib``, ``numpy.libs``,
``scipy.libs``, …) et les résout au runtime grâce au ``LD_LIBRARY_PATH`` posé
par le bootloader. linuxdeploy (bundling AppImage de Tauri) ne s'appuie pas
sur ce mécanisme et ne retrouve donc pas ``libtorch.so``, ``libgfortran-*``,
etc. → ``ERROR: Could not find dependency``.

Deux problèmes sont corrigés ici :

1. Les symlinks raccourcis créés par PyInstaller à la racine ``_internal``
   (``libtorchaudio.so -> torchaudio/lib/...``, ``libopenblas*``, …) sont
   « matérialisés » (le binaire est copié à la racine) parce que Tauri suit les
   symlinks lors de la copie des ressources : un RPATH calculé d'après la cible
   deviendrait faux une fois le fichier déplacé sur place.
2. Un ``RPATH`` relatif (``$ORIGIN``) est posé sur tous les ELF pour que
   linuxdeploy (et le chargeur dynamique) retrouvent ces bibliothèques.

Usage :
    python3 scripts/patch_engine_rpath.py [chemin_vers_engine]
"""
import os
import shutil
import subprocess
import sys

# Sous-dossiers (relatifs à la racine ``_internal``) qui concentrent les
# grosses bibliothèques natives vendues (torch / numpy / scipy).
SUBDIR_LIBS = [
    "torch/lib",
    "torchaudio/lib",
    "numpy.libs",
    "scipy.libs",
    "scikit_learn.libs",
]

# Taille max pour matérialiser un symlink : au-delà on conserve le lien (ex.
# libtriton.so ~460 Mo) — ces fichiers sont trop gros pour patchelf de toute
# façon et ne sont pas à l'origine des erreurs de linuxdeploy.
MAX_DEREF_SIZE = 100 * 1024 * 1024


def find_patchelf() -> str | None:
    """Retrouve un binaire ``patchelf`` sur le PATH ou via $PATCHELF."""
    over = os.environ.get("PATCHELF")
    if over and os.path.isfile(over):
        return over
    return shutil.which("patchelf")


def is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def rel_from_root(root: str, real_dir: str) -> str:
    """Chemin relatif ``$ORIGIN/<up>`` du répertoire ``real_dir`` vers ``root``."""
    rel = os.path.relpath(real_dir, root)
    if rel in (".", ""):
        return "$ORIGIN"
    return "$ORIGIN/" + "/".join([".."] * len(rel.split(os.sep)))


def dereference_symlinks(root: str) -> int:
    """Matérialise les symlinks ELF de la racine ``_internal`` (évite un RPATH
    basé sur l'emplacement de la cible → faux une fois le fichier sur place)."""
    count = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.islink(path):
            continue
        target = os.path.realpath(path)
        if not target.startswith(root):
            continue
        if not is_elf(target):
            continue
        if os.path.getsize(target) > MAX_DEREF_SIZE:
            print(f"  conserve le lien {name} (taille > {MAX_DEREF_SIZE // 1048576} Mo)")
            continue
        os.remove(path)
        shutil.copy2(target, path)
        count += 1
    if count:
        print(f"Symlinks matérialisés : {count}")
    return count


def patch_engine(engine: str) -> int:
    root = os.path.abspath(os.path.join(engine, "_internal"))
    if not os.path.isdir(root):
        print(f"_internal introuvable : {root}", file=sys.stderr)
        return 1
    patchelf = find_patchelf()
    if not patchelf:
        print("patchelf introuvable sur le PATH. Installez-le (ex. 'apt install "
              "patchelf') ou définissez $PATCHELF.", file=sys.stderr)
        return 1

    dereference_symlinks(root)

    patched = 0
    skipped = 0
    seen = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            if not is_elf(real):
                continue
            real_dir = os.path.dirname(real)
            if not real_dir.startswith(root):
                continue
            up = rel_from_root(root, real_dir)
            entries = ["$ORIGIN", up]
            for sub in SUBDIR_LIBS:
                if os.path.isdir(os.path.join(root, sub)):
                    entries.append(os.path.join(up, sub))
            rpath = ":".join(entries)
            try:
                subprocess.run([patchelf, "--set-rpath", rpath, real],
                               check=True, capture_output=True)
                patched += 1
            except subprocess.CalledProcessError as exc:
                skipped += 1
                print(f"  SKIP {os.path.relpath(real, root)}: "
                      f"{(exc.stderr or b'').decode(errors='replace').strip()}",
                      file=sys.stderr)
    print(f"RPath posé sur {patched} ELF (skipped {skipped}) dans {root}")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "src-tauri/resources/engine"
    sys.exit(patch_engine(target))
