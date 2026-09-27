#!/usr/bin/env bash
# =============================================================================
# build_desktop.sh — Chaîne de production du bureau Guitar Lab
#
#  1. Active le venv .venv_build
#  2. Recompile le moteur autonome PyInstaller -> src-tauri/resources/engine/
#     (inclut le patch RPATH $ORIGIN pour que linuxdeploy resolve torch & co.)
#  3. Lance `npx @tauri-apps/cli build` (AppImage + deb)
#  4. Affiche le chemin et la taille de l'AppImage générée
#
# Prérequis machine :
#   - patchelf  (requis par linuxdeploy-plugin-gstreamer pour le bundleMediaFramework)
#   - node/npx  (pour le CLI Tauri)
#   - rust       (toolchain cargo)
# =============================================================================
set -euo pipefail

# Chemin absolu de la racine du projet (un niveau au-dessus de ce script).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

log()  { printf '\033[1;36m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[build]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[build]\033[0m %s\n' "$*" >&2; exit 1; }

VENV="$ROOT/.venv_build"
ENGINE_TARGET="$ROOT/src-tauri/resources/engine"

# -----------------------------------------------------------------------------
# 0. Vérifications d'environnement
# -----------------------------------------------------------------------------
log "Vérification de l'environnement…"
[ -x "$VENV/bin/python" ] || fail "venv introuvable : $VENV/bin/python (créez-le avec 'python3 -m venv .venv_build' puis installez requirements.txt)."
[ -f "$ROOT/guitarlab-engine.spec" ] || fail "spec PyInstaller introuvable : guitarlab-engine.spec"
command -v npx >/dev/null 2>&1 || fail "npx introuvable. Installez Node.js (npm) pour le CLI Tauri."

# patchelf : requis par linuxdeploy-plugin-gstreamer (bundleMediaFramework: true)
ensure_patchelf() {
  if command -v patchelf >/dev/null 2>&1 || [ -n "${PATCHELF:-}" ]; then
    return 0
  fi
  warn "patchelf introuvable. Tentative d'installation…"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y patchelf || true
  fi
  if command -v patchelf >/dev/null 2>&1; then
    return 0
  fi
  fail "patchelf introuvable et non installable. Installez-le (ex. 'sudo apt install patchelf') et relancez."
}
ensure_patchelf

# -----------------------------------------------------------------------------
# 1. Active le venv .venv_build
# -----------------------------------------------------------------------------
log "Activation du venv .venv_build"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
PYINSTALLER="$VENV/bin/pyinstaller"
PYTHON="$VENV/bin/python"

# -----------------------------------------------------------------------------
# 2. Recompile le moteur PyInstaller -> src-tauri/resources/engine/
# -----------------------------------------------------------------------------
log "Nettoyage des artefacts moteur précédents"
rm -rf "$ROOT/dist/guitarlab-engine" "$ENGINE_TARGET"
mkdir -p "$ROOT/src-tauri/resources"

log "Compilation du moteur autonome (PyInstaller)…"
"$PYINSTALLER" "$ROOT/guitarlab-engine.spec"

log "Copie du moteur vers src-tauri/resources/engine/"
mv "$ROOT/dist/guitarlab-engine" "$ENGINE_TARGET"

log "Patch des RPATH \$ORIGIN (compatibilité linuxdeploy/torch)"
"$PYTHON" "$ROOT/scripts/patch_engine_rpath.py" "$ENGINE_TARGET"

# -----------------------------------------------------------------------------
# 3. Lance le build Tauri (AppImage + deb)
# -----------------------------------------------------------------------------
log "Build Tauri (npx @tauri-apps/cli build)…"
( cd "$ROOT/src-tauri" && npx @tauri-apps/cli build )

# -----------------------------------------------------------------------------
# 4. Rapporte l'AppImage générée
# -----------------------------------------------------------------------------
APPIMAGE=$(find "$ROOT/src-tauri/target/release/bundle/appimage" -maxdepth 1 -name '*.AppImage' -print -quit 2>/dev/null)
if [ -n "$APPIMAGE" ]; then
  SIZE=$(du -h "$APPIMAGE" | cut -f1)
  log "✅ AppImage générée :"
  log "   Chemin : $APPIMAGE"
  log "   Taille : $SIZE"
else
  warn "Aucune AppImage trouvée dans src-tauri/target/release/bundle/appimage/"
  warn "Vérifiez le journal Tauri ci-dessus (le build a pu échouer)."
  exit 1
fi

log "Terminé."
