#!/usr/bin/env bash
# Lance l'API FastAPI de Guitar Lab en arrière-plan pour l'application desktop.
# Utilisé par le wrapper Tauri (src-tauri/src/lib.rs).
set -euo pipefail

# Répertoire du script = racine du projet (main.py, services/, static/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Active le virtualenv local s'il existe, sinon réutilise python3 du PATH.
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif [ -d "venv" ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"

# L'environnement AppRun de l'AppImage peut injecter PYTHONHOME / PYTHONPATH
# qui cassent le python système (ex: « Failed to import encodings »).
# On les neutralise avant de lancer le serveur.
unset PYTHONHOME
unset PYTHONPATH
export PYTHONUNBUFFERED=1

# En desktop, on préfère stocker les données utilisateur dans un répertoire
# inscriptible (pas dans l'AppImage montée en lecture seule).
export DATA_DIR="${DATA_DIR:-$HOME/.local/share/guitarlab/data}"

echo "[run_backend] Lancement de Guitar Lab sur ${HOST}:${PORT} (data=${DATA_DIR})"
exec python3 main.py
