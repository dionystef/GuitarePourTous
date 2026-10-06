"""Configuration pytest pour la suite de tests Guitar Lab.

IMPORTANT : ce fichier est importé AVANT tout module de test. Il :

1. Injecte la racine du dépôt dans ``sys.path`` (pour ``import main`` / ``services.*``) ;
2. Fixe ``DATA_DIR`` sur un répertoire temporaire isolé AVANT l'import de
   ``main`` (``main.py`` lit ``DATA_DIR`` au moment de l'import pour créer le
   dossier et construire ``mgr``).
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Répertoire de données jetable, créé une fois pour toute la session de test.
_TEST_DATA = tempfile.mkdtemp(prefix="guitarlab_tests_")
os.environ["DATA_DIR"] = _TEST_DATA
