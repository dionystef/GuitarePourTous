# 🎸 Guitar Lab — Labo de répétition guitare (Docker Compose)

Transformez une **URL YouTube** ou un **fichier audio** en session de répétition
interactive : lecteur **multi-pistes (stems)** synchronisé, **grille d'accords**
synchronisée façon Chordify, et **solo extrait en MIDI + tablature**.

Interface **SPA / PWA ready**, thème sombre typé studio, servie directement par
FastAPI (Vanilla JS, aucun build front nécessaire).

---

## 1. Stack

| Brique | Techno |
|---|---|
| API / orchestration | Python 3.10 · FastAPI · Uvicorn · WebSocket |
| Téléchargement | yt-dlp (meilleure piste audio) |
| Séparation de sources | **Demucs** `htdemucs_6s` (vocals · drums · bass · guitar · piano · other) |
| Beats + accords | **madmom** (CRF/RNN) avec repli automatique sur **librosa** (chroma CENS) |
| Solo → MIDI | **Spotify basic-pitch** (ONNX), tablature ASCII générée côté serveur |
| GPU | Optionnel (CUDA 12.1), **fallback CPU automatique** au démarrage |
| Persistance | `./data/<hash_id>/` — metadata.json par morceau + index scanné au boot |

### Pipeline d'un morceau

```
 URL YouTube ──► yt-dlp ──► source/audio.wav (44,1 kHz stéréo)
 upload ───────► normalisation ──────────────────────────────┘
        │
        ├─► Demucs htdemucs_6s ──► stems/*.mp3  (mix, guitar, bass, drums,
        │     (fallback htdemucs si échec)         vocals, piano, other)
        │
        ├─► madmom CRF / librosa ──► chords.json (bpm, bars, temps, accords)
        │
        └─► basic-pitch (stem guitare) ──► guitar_solo.mid + guitar_tab.txt
```

---

## 2. Démarrage rapide (CPU)

Prérequis : **Docker** (ou Docker Desktop) , puis :

```bash
cd GuitarePourTous
docker compose up --build
```

Ouvrir : [http://localhost:8000](http://localhost:8000)

- La 1ʳᵉ fois, les modèles (Demucs ~300 Mo, madmom, basic-pitch) sont
  téléchargés dans le volume nommé `model_cache` (persisté entre les rebuilds).
- Les morceaux traités sont stockés dans `./data/` (monté en volume).
- Sans GPU, `DEVICE=auto` bascule automatiquement sur le **CPU**
  (plus lent : comptez 3–8 min par morceau selon la durée).

> ⚠ yt-dlp évolue vite : si un téléchargement échoue, rafraîchissez-le avec
> `docker compose build --pull guitarlab` (ou ouvrez un shell dans le conteneur :
> `docker compose exec guitarlab pip install -U yt-dlp`).

---

## 3. Accélération GPU (NVIDIA / CUDA)

### 3.1 Préparer l'hôte

```bash
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Vérifier : `docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi`

### 3.2 Lancer avec la surcouche GPU

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

- Le fichier `docker-compose.gpu.yml` écrase l'index PyTorch (`+cu121`), force
  `DEVICE=cuda` et réserve les GPU.
- Sans surcouche, le bloc `deploy.resources.reservations.devices` reste
  **commenté / documenté** dans `docker-compose.yml` : le pipeline tourne alors
  en CPU, y compris si le conteneur GPU est démarré sur une machine sans GPU
  (la détection `torch.cuda.is_available()` gère le basculement).

---

## 4. Utilisation

### Front (SPA)

1. **Bibliothèque** — collez une URL YouTube ou upload un fichier audio
   (MP3/WAV/M4A/FLAC/OGG), cliquez sur **Décortiquer**.
2. Le **terminal de progression** affiche les logs temps réel (WebSocket,
   repli polling). En fin de traitement, le labo s'ouvre automatiquement.
3. **Console de répétition** :
   - Transport : ▶ / ⏸ / ■, seekbar globale, **vitesse 0.5×–1.0× sans changer
     de hauteur** (`preservesPitch`), **métronome** accentué tous les 4 temps.
   - **Boucle A/B** : boutons `A`/`B` de la barre de transport ou sur chaque
     mesure de la grille (cliquer une mesure = y sauter).
   - **Mixeur 6 canaux** : faders, `M`ute / `S`olo, synchronisation stricte des
     pistes (contrôle anti-dérive 200 ms + correction de débit).
   - **Grille d'accords** : mesure active et temps courant surlignés pendant la
     lecture ; **diagramme manche** (doigtés / barrés) de l'accord courant.
   - **Tab** : `Tablature` → affiche `guitar_tab.txt` ; `MIDI` → télécharge
     `guitar_solo.mid`.

### API

| Méthode | Route | Description |
|---|---|---|
| `POST` | `/api/process` | multipart : `url` ou `file` → `{id}` |
| `GET` | `/api/status/{id}` | `{status, progress, logs[]}` |
| `WS` | `/api/ws/{id}` | poussée `<snapshot>` en temps réel (ping → pong) |
| `GET` | `/api/tracks` | liste des morceaux (bibliothèque) |
| `GET` | `/api/tracks/{id}` | détail + `chords` (bars/times/accords) |
| `DELETE` | `/api/tracks/{id}` | suppression du morceau |
| `POST` | `/api/tracks/{id}/bar-offset` | aligne le temps 1 (anacrouse) : `offset` 0..3 → `bar_offset` |
| `POST` | `/api/tracks/{id}/structure-start` | définit la mesure de début de structure (retour à la ligne) |
| `POST` | `/api/tracks/{id}/structure` | JSON `{line_breaks[], sections{}}` : sauts de ligne + tags de section |
| `GET` | `/api/health` | `{status, device}` |
| `GET` | `/data/{id}/…` | médias servis en statique (Range supporté → seek) |

---

## 5. Arborescence

```
GuitarePourTous/
├── Dockerfile                  # ffmpeg + libsndfile + torch (CPU/CUDA by ARG)
├── docker-compose.yml          # CPU par défaut, bloc GPU documenté
├── docker-compose.gpu.yml      # surcouche CUDA (device reservations)
├── requirements.txt            # versions stables des dépendances Python
├── main.py                     # API FastAPI + orchestrateur asynchrone
├── services/
│   ├── downloader.py           # yt-dlp / upload → source/audio.wav
│   ├── separator.py            # Demucs → stems/*.mp3 + guitar.wav
│   ├── chord_analyzer.py       # madmom (repli librosa) → chords.json
│   └── solo_transcriber.py     # basic-pitch → MIDI + tablature
├── static/
│   ├── index.html              # SPA
│   ├── app.js                  # lecteur, mixeur, grille, affichage
│   ├── style.css               # thème DAW sombre
│   ├── manifest.webmanifest    # PWA
│   ├── icon.svg
│   └── service-worker.js       # cache PWA
└── data/                       # bibliothèque locale (volume monté)
    └── <hash_id>/
        ├── source/ (raw, audio.wav, audio.mp3)
        ├── stems/  (mix, vocals, drums, bass, guitar, other … .mp3)
        ├── chords.json
        ├── guitar_solo.mid / guitar_tab.txt
        └── metadata.json + status.json
```

---

## 6. Développement sans Docker

Python 3.10 (3.9+2 possible avec quelques épingles ajustées) :

```bash
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip setuptools wheel cython
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.2.2 torchaudio==2.2.2
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

(Installez `ffmpeg` et `libsndfile1` sur la machine via votre gestionnaire de
paquets.)

---

## 7. Pousser sur Git

```bash
git init -b main
git add .
git commit -m "🎸 Guitar Lab : pipeline stems + accords + solo, Docker Compose"
git remote add origin git@github.com:<VOTRE_USER>/GuitarePourTous.git
git branch -M main
git push -u origin main
```

N'oubliez pas d'ajouter un secret GitHub si le dépôt est privé. Le `.gitignore`
exclut `data/` (bibliothèque locale) et `__pycache__/` ; le `.dockerignore`
allège le contexte de build.

---

## 8. Dépannage

| Symptôme | Solution |
|---|---|
| **Build KO sur madmom** | Le Dockerfile compile madmom en `--no-build-isolation` avec `Cython==0.29.36` (Cython 3 casserait ce paquet de 2020). En dernier recours, commentez la ligne `madmom` dans `requirements.txt` : la détection d'accords bascule automatiquement sur **librosa**. |
| **Téléchargement YouTube en échec** | yt-dlp obsolète → `pip install -U yt-dlp` dans le conteneur ou rebuild. |
| **Demucs OOM / trop lent** | Réduisez la charge : `DEMUCS_SHIFTS=0`, `DEMUCS_SEGMENT=6` (variables du service dans compose). |
| **GPU non utilisé alors qu'il existe** | Vérifiez `nvidia-smi` dans l'hôte et utilisez la surcouche GPU (section 3). |
| **Mémoire disque** | Chaque morceau ≈ 60–120 Mo (wavs tuiles + MP3). Supprimez via l'UI (`Supprimer`) ou `./data/*`. |
| **Chords fantaisistes au début** | Normal : le 1er temps est souvent une intro/instrumentale — affinage possible en post (fichier `chords.json`). |