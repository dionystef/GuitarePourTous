# 🎸 Guitar Lab — Studio & Labo de répétition Guitare (IA) — V1

[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Demucs](https://img.shields.io/badge/Demucs-v4_htdemucs__6s-FF6F00)](https://github.com/facebookresearch/demucs)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Guitar Lab (V1)** transforme n'importe quel morceau (**URL YouTube** ou **fichier audio uploadé**) en une session de répétition interactive complète pour les musiciens et guitaristes.

Grâce à des modèles d'intelligence artificielle exécutés en local, l'application isole les instruments, détecte le tempo et synchronise la grille d'accords en temps réel sur un lecteur multipiste avec manche de guitare virtuel.

> [!NOTE]
> **Statut V1 :** La version 1.0 se concentre sur l'isolation parfaite des pistes (6 stems), la grille d'accords synchronisée et le studio de répétition multipiste. La brique d'extraction de solos (MIDI & tablatures via Spotify Basic Pitch) est préparée dans le code et documentée pour la prochaine mise à jour (V2).

---

## 🌟 Fonctionnalités clés (V1)

### 🧠 Analyse & Séparation par Intelligence Artificielle
- **Séparation de sources 6 pistes (Stems)** via **Demucs v4** (`htdemucs_6s`) :
  - 🎸 Guitare
  - 🎤 Voix
  - 🥁 Batterie
  - 🎸 Basse
  - 🎹 Piano
  - 🎼 Autre / Synthés / Cuivres
- **Détection automatique du tempo (BPM) & de la métrique** (4/4, etc.).
- **Détection des accords synchronisés mesure par mesure** (façon Chordify) via **Madmom** (réseaux récurrents RNN/CRF) avec repli intelligent sur **Librosa** (chroma CENS).

### 🎛️ Studio de Répétition & Console de Mixage (Web Audio API)
- **Mixeur 6 canaux synchronisé à la milliseconde** : faders de volume indépendants, boutons **Mute** et **Solo** pour isoler la guitare ou retirer la guitare du morceau original (*backing track* instantané).
- **Ralenti sans déformation de hauteur (0.5× à 1.0×)** : travaillez les passages rapides à vitesse réduite sans changer la tonalité (`preservesPitch`).
- **Boucles A/B au millième de seconde** : isolez une mesure, un riff ou une phrase musicale et répétez-la en boucle automatique.
- **Métronome dynamique synchronisé** : clic audio avec accentuation du premier temps de chaque mesure.
- **Grille d'accords interactive** : défilement en direct avec mise en avant du temps et de la mesure courante, clic sur une mesure pour s'y téléporter.
- **Visualiseur de manche (Fretboard)** : affichage interactif des diagrammes d'accords (doigtés, cordes à vide, notes étouffées et barrés).
- **Éditeur de structure musicale** :
  - Décalage d'anacrouse (*bar offset*) pour aligner parfaitement le temps 1.
  - Définition du début de structure et sauts de ligne personnalisés.
  - Annotation des sections (*Intro, Couplet, Refrain, Solo, Pont, Outro*).

### 🎨 Confort & Interface
- **Thèmes interchangeables** : basculez en 1 clic entre le thème **Studio Dark** (néons bleu/violet) et le thème **Xbox Microsoft** (vert néon `#107C10` / `#22C55E`).
- **Animation interactive** : petit guitariste animé s'adaptant à la palette de couleurs.
- **PWA Ready** : installable sur desktop et mobile sans passer par un store.
- **100% Local & Privé** : aucun abonnement tiers nécessaire, vos morceaux et calculs restent sur votre machine.

---

## 🚀 Installation & Démarrage rapide (Docker)

Le moyen le plus simple et recommandé pour lancer Guitar Lab est **Docker Compose**. Tout l'environnement (Python, PyTorch, FFmpeg, modèles IA) est encapsulé.

### Prérequis
- Installer **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** (Windows / macOS) ou **Docker Engine + Docker Compose** (Linux).
- Au moins 8 Go de RAM recommandée (16 Go conseillés pour Demucs).

---

### 🐧 Option 1 : Installation sur Linux

1. **Cloner le dépôt :**
   ```bash
   git clone https://github.com/<VOTRE_USER>/GuitarePourTous.git
   cd GuitarePourTous
   ```

2. **Lancer l'application (Mode CPU par défaut) :**
   ```bash
   docker compose up --build
   ```

3. **Accéder à l'interface :**
   Ouvrez votre navigateur sur **[http://localhost:8005](http://localhost:8005)**.

> [!TIP]
> **Accélération GPU NVIDIA (CUDA) sous Linux :**
> Si vous disposez d'une carte graphique NVIDIA avec `nvidia-container-toolkit` configuré :
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
> ```
> Le traitement passera de ~4 minutes par morceau à moins de 40 secondes !

---

### 🪟 Option 2 : Installation sur Windows

1. **Prérequis Windows :**
   - Assurez-vous que **Docker Desktop** est lancé avec le moteur **WSL 2** activé (*Settings > General > Use the WSL 2 based engine*).
   - Ouvrez un terminal **PowerShell** ou un terminal **WSL Ubuntu**.

2. **Cloner et lancer :**
   ```powershell
   git clone https://github.com/<VOTRE_USER>/GuitarePourTous.git
   cd GuitarePourTous
   docker compose up --build
   ```

3. **Accéder à l'application :**
   Rendez-vous sur **[http://localhost:8005](http://localhost:8005)**.

> [!NOTE]
> **Remarque réseau sous Windows :**
> Le fichier `docker-compose.yml` utilise `network_mode: host` pour contourner certains blocages VPN sous Linux.
> Si sous Windows avec Docker Desktop vous n'arrivez pas à joindre `http://localhost:8005` :
> 1. Ouvrez `docker-compose.yml`.
> 2. Commentez la ligne `network_mode: host` (`# network_mode: host`).
> 3. Décommentez la publication de ports :
>    ```yaml
>    ports:
>      - "8005:8005"
>    ```
> 4. Relancez `docker compose up`.

---

## 📖 Mode d'emploi pas à pas

```
   ┌─────────────────────────────────────────────────────────────┐
   │ 1. Coller une URL YouTube ou uploader un fichier audio       │
   └──────────────────────────────┬──────────────────────────────┘
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 2. Séparation Demucs (6 pistes) + Madmom/Librosa (Accords)   │
   └──────────────────────────────┬──────────────────────────────┘
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 3. Studio de répétition ouvert :                            │
   │    - Couper la guitare originale (Solo/Mute)                │
   │    - Ralentir à 0.75x sans changer la hauteur               │
   │    - Boucler une mesure ou une phrase (Boucle A/B)          │
   │    - Suivre les accords en temps réel sur le manche         │
   └─────────────────────────────────────────────────────────────┘
```

1. **Ajouter un morceau :**
   - **URL YouTube** : collez le lien dans le champ prévu et cliquez sur **Décortiquer**.
   - **Fichier local** : cliquez sur **⬆ Upload audio** (WAV, MP3, FLAC, M4A, OGG), puis sur **Décortiquer**.
2. **Suivre le traitement :**
   - Un terminal s'affiche avec la progression en temps réel (téléchargement, séparation des stems, analyse harmonique).
   - Les modèles IA sont automatiquement mis en cache dans un volume persistant lors du premier lancement.
3. **Travailler son instrument dans le lecteur :**
   - Utilisez le **Mixeur** pour monter/baisser la guitare ou la couper pour jouer par-dessus.
   - Ajustez la vitesse avec le curseur **0.5× – 1.0×**.
   - Activez le bouton **[A]** puis **[B]** sur la barre de transport ou cliquez sur les mesures de la grille pour travailler un passage spécifique.
   - Suivez les diagrammes d'accords sur le manche virtuel synchronisé.
4. **Changer de thème visuel :**
   - Cliquez sur le bouton **🎮 Xbox / Studio** en haut à gauche de l'écran pour basculer instantanément de style.

---

## 🏗️ Architecture & Pipeline Technique

```
                              [Entrée Audio]
                       (YouTube yt-dlp ou Upload)
                                    │
                                    ▼
                          [source/audio.wav]
                         (PCM 44,1 kHz Stéréo)
                                    │
       ┌────────────────────────────┴────────────────────────────┐
       ▼                                                         ▼
[Demucs htdemucs_6s]                                   [Madmom CRF / Librosa]
       │                                                         │
       ▼                                                         ▼
   stems/*.mp3                                              chords.json
(Guitare, Basse,                                         (BPM, Mesures,
 Batterie, Voix,                                          Temps, Accords)
 Piano, Autre)                                                   │
       │                                                         │
       └────────────────────────────┬────────────────────────────┘
                                    │
                                    ▼
                         [Lecteur Web Audio API]
                    Multi-stems synchro, Fretboard,
                       Boucles A/B & Transport
                                    │
               (Roadmap V2 : Spotify basic-pitch solo MIDI / Tab)
```

### Pile technologique :
- **Backend** : Python 3.10, FastAPI, Uvicorn, WebSockets.
- **Traitement Audio & IA** :
  - [Demucs](https://github.com/facebookresearch/demucs) (v4 Hybrid Transformer 6 sources).
  - [Madmom](https://github.com/CPJKU/madmom) (Deep Learning pour détection du rythme et des accords).
  - [Librosa](https://github.com/librosa/librosa) (Analyse spectrale & CENS chromagrams).
  - [FFmpeg](https://ffmpeg.org/) (conversion et normalisation audio).
  - *(En préparation V2)* : [Spotify Basic Pitch](https://github.com/spotify/basic-pitch) (transcription de solo).
- **Frontend** :
  - Single Page Application (SPA) en **JavaScript Vanilla** (aucun bundler/npm requis).
  - Web Audio API (moteur audio synchrone multi-tampons, anti-dérive).
  - CSS3 moderne avec variables dynamiques & Thème Switcher.

---

## 📂 Structure du projet

```
GuitarePourTous/
├── Dockerfile                  # Environnement d'exécution (FFmpeg, Libsndfile, PyTorch)
├── docker-compose.yml          # Déploiement standard CPU (réseau optimisé)
├── docker-compose.gpu.yml      # Extension NVIDIA CUDA 12.1
├── requirements.txt            # Dépendances Python verrouillées
├── main.py                     # API FastAPI, WebSocket & orchestrateur de pipeline
│
├── services/                   # Modules métier
│   ├── downloader.py           # Téléchargement YouTube (yt-dlp) & normalisation
│   ├── separator.py            # Séparation de pistes Demucs 6s
│   ├── chord_analyzer.py       # Analyse BPM & accords (Madmom + Librosa)
│   ├── solo_transcriber.py     # Module de transcription de solo (prévu V2)
│   └── library.py              # Gestion du stockage local et des métadonnées
│
├── static/                     # Interface Web (SPA / PWA)
│   ├── index.html              # Vue principale
│   ├── style.css               # Feuilles de style (Studio sombre & Xbox Microsoft)
│   ├── app.js                  # Moteur audio, mixeur, affichage des accords et boucles
│   ├── manifest.webmanifest    # Configuration PWA
│   └── icon.svg                # Logo de l'application
│
└── data/                       # Stockage persistant des morceaux (volume Docker)
    └── <track_id>/
        ├── source/             # Audio original (wav / mp3)
        ├── stems/              # Pistes séparées (guitar, bass, drums, vocals...)
        ├── chords.json         # Grille d'accords temporelle
        └── metadata.json       # Métadonnées, structure & réglages utilisateur
```

---

## 🔧 Dépannage & FAQ

<details>
<summary><b>1. Pourquoi le bouton renvoie vers un morceau existant au lieu de relancer ?</b></summary>

Guitar Lab intègre une protection anti-doublon pour éviter de recalculer inutilement un morceau déjà présent dans votre bibliothèque. Si vous collez une URL déjà traitée, l'application ouvre directement le labo de répétition. Pour recalculer un morceau, supprimez-le simplement de la bibliothèque en cliquant sur la croix **✕** rouge de sa carte.
</details>

<details>
<summary><b>2. Une erreur survient lors du téléchargement d'une vidéo YouTube</b></summary>

YouTube met régulièrement à jour ses protections. Si `yt-dlp` échoue à télécharger un flux :
```bash
docker compose exec guitarlab pip install -U yt-dlp
```
Ou reconstruisez l'image sans le cache : `docker compose build --no-cache guitarlab`.
</details>

<details>
<summary><b>3. Combien de temps prend l'analyse d'un morceau ?</b></summary>

- **Sur GPU (CUDA)** : ~30 à 60 secondes pour une chanson complète de 4 minutes.
- **Sur CPU moderne** : ~3 à 6 minutes selon la puissance de votre processeur (Demucs réalise plusieurs millions d'inférences).
</details>

<details>
<summary><b>4. Espace disque et gestion des fichiers</b></summary>

Chaque morceau traité pèse environ 60 à 100 Mo (fichier audio WAV normalisé + 6 stems MP3 haute fidélité). Vous pouvez purger les morceaux inutilisés directement depuis l'interface web ou en supprimant les dossiers correspondants dans `./data/`.
</details>

---

## 📄 Licence

Ce projet est distribué sous licence MIT. Les modèles sous-jacents (Demucs, Madmom) restent soumis à leurs licences open-source respectives.