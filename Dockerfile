# -----------------------------------------------------------------------------
# Guitar Lab — image de traitement audio
#
# Build CPU (défaut) :
#   docker build -t guitarlab:latest .
#
# Build GPU CUDA 12.1 :
#   docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121 \
#                -t guitarlab:gpu .
#
# Le choix CPU/GPU se décide **au build** (couche torch). Le fallback CPU
# automatique à l'exécution est géré par le backend (résolution DEVICE=auto).
# -----------------------------------------------------------------------------

ARG PYTHON_VERSION=3.10

FROM python:${PYTHON_VERSION}-slim

ARG DEBIAN_FRONTEND=noninteractive

ENV \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    DATA_DIR=/app/data \
    DEVICE=auto \
    TORCH_HOME=/models/torch \
    DEMUCS_CACHE=/models/torch/demucs \
    HUGGINGFACE_HUB_CACHE=/models/huggingface \
    BASIC_PITCH_CHECKPOINT_DIR=/models/basic_pitch

# Outils système : ffmpeg (conversion/encodage), libsndfile (soundfile),
# libgomp1 (OpenMP de torch), compilateur C (madmom/cython).
RUN apt-get update -o Acquire::Retries=5 -o Acquire::http::Timeout=20 \
        -o Acquire::ForceIPv4=true \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        libgomp1 \
        libsndfile1 \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Éléments de build Python — Cython 0.29 (compatible madmom 2020) doit être
# visible lors de la compilation de madmom (voir --no-build-isolation).
# numpy/scipy sont pré-installés ici pour que la compilation de madmom
# (--no-build-isolation) puisse s'appuyer sur l'environnement courant.
# setuptools est épinglé <70 : à partir de la 70.x, `pkg_resources` (utilisé
# par librosa & madmom) a été retiré de setuptools.
RUN python -m pip install --upgrade pip "setuptools<70" wheel \
        "cython==0.29.36" \
        "numpy==1.24.4" \
        "scipy==1.10.1"

# PyTorch — installé séparément pour pouvoir choisir CPU / CUDA au build.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN python -m pip install --index-url "${TORCH_INDEX_URL}" \
        torch==2.2.2 torchaudio==2.2.2

WORKDIR /app

COPY requirements.txt .
# --no-build-isolation : madmom doit compiler avec le Cython/environnement
# déjà installé (numpy, scipy), sinon pip l'isole et ne trouve plus Cython.
RUN python -m pip install --no-build-isolation -r requirements.txt

# Code applicatif.
COPY services ./services
COPY static ./static
COPY main.py .

# Répertoires de données & de cache des modèles (volume nommé).
RUN mkdir -p /app/data /models \
    && chmod -R a+rwX /models /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]