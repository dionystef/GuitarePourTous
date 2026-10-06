"""Tests des constantes de sécurité / robustesse (timeouts, limites upload/WS).

Ces constantes sont lues depuis l'environnement au moment de l'import ; on
vérifie leurs valeurs par défaut et leur caractère borné (aucune valeur
infinie/négative qui rendrait les garde-fous inopérants).
"""
import services.downloader as dl
import services.lyrics_provider as lp
import main


def test_ytdlp_socket_timeout_default_is_bounded():
    assert dl.SOCKET_TIMEOUT > 0
    assert dl.SOCKET_TIMEOUT <= 300  # borné, pas de blocage infini


def test_job_timeout_default_is_positive():
    assert main.JOB_TIMEOUT > 0
    # Valeur robuste : ni 0 (annulation immédiate) ni déraisonnable.
    assert 60 <= main.JOB_TIMEOUT <= 24 * 3600


def test_upload_limit_default():
    assert main.MAX_UPLOAD > 0
    # 500 Mo par défaut.
    assert main.MAX_UPLOAD == 500 * 1024 * 1024


def test_chunk_upload_size_positive():
    assert main.CHUNK_UPLOAD > 0


def test_ws_max_size_default():
    assert main.WS_MAX_SIZE > 0
    assert main.WS_MAX_SIZE == 4 * 1024 * 1024


def test_transcribe_timeout_bounded():
    assert lp.TRANSCRIBE_TIMEOUT > 0


def test_audio_allowlist_non_empty_and_lowercase():
    assert dl.ALLOWED_AUDIO_EXT  # non vide
    assert all(ext.startswith(".") and ext == ext.lower()
               for ext in dl.ALLOWED_AUDIO_EXT)


def test_csp_constant_not_empty():
    assert main.CSP.strip()
    assert "object-src 'none'" in main.CSP
    assert "default-src 'self'" in main.CSP
