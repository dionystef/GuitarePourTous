r"""Tests MISSION : logique du script de déploiement atomique.

Le job ``deploy`` de ``.github/workflows/ci.yml`` exécute un script Bash distant
(heredoc ``REMOTE``). Deux correctifs y ont été apportés :

  1. **Symlink de la bibliothèque** — ``rm -rf "$RELEASE/data"`` avant
     ``ln -sfn "$APP_ROOT/data" "$RELEASE/data"`` : le ``git clone`` recrée
     ``$RELEASE/data`` (via ``data/.gitkeep`` suivi) et, sans suppression, un
     symlink imbriqué ``$RELEASE/data/data`` serait créé et l'application
     monterait une bibliothèque vide.
  2. **Polling du healthcheck configurable** — ``DEPLOY_HEALTH_TIMEOUT_SECS``
     (défaut 300 s) détermine le nombre d'itérations ; on réutilise l'état
     Docker ``healthy``/``unhealthy`` (qui respecte le ``start_period`` de 30 s
     du healthcheck Compose) avec repli sur ``curl /api/health``, afin d'éviter
     les faux rollbacks dus au boot torch/demucs.
  3. **Transport du dépôt en base64 via stdin** — ``DEPLOY_REPO_URL`` (qui peut
     contenir une apostrophe) est encodé en base64 côté local et injecté en tête
     du heredoc envoyé à ``bash -s`` : insensible au quoting et jamais exposé sur
     la ligne de commande ``ssh`` (donc invisible dans ``ps``).

Ces tests **exécutent réellement** le script distant dans un bac à sable
temporaire, avec des commandes ``git``/``docker``/``curl``/``sleep`` factices
sur le ``PATH`` (aucun SSH/Docker réel requis) :
  * ``git clone`` recrée systématiquement ``<dest>/data/.gitkeep`` (comme le
    vrai dépôt) ;
  * ``curl`` simule la disponibilité progressive (ou jamais) de ``/api/health`` ;
  * ``docker inspect``/``compose`` simulent l'état Docker et le répertoire de
    travail de la bascule.

On vérifie ainsi que la bibliothèque est correctement symlinkée, que l'état
Docker est réutilisé (aucun faux rollback, rollback immédiat sur ``unhealthy``),
que le délai de polling est configurable, et que l'URL du dépôt survit au
quoting via le transport base64.
"""
import base64
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _local_run_block() -> str:
    """Extrait le bloc ``run`` du step de déploiement (partie locale au runner)."""
    workflow = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    return workflow["jobs"]["deploy"]["steps"][1]["run"]


def _extract_remote_script() -> str:
    """Extrait le corps du script distant (entre ``<< 'REMOTE'`` et ``REMOTE``)."""
    if not WORKFLOW.exists():
        pytest.fail(f"Workflow introuvable : {WORKFLOW}")
    workflow = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    run = workflow["jobs"]["deploy"]["steps"][1]["run"]
    lines = run.splitlines()
    start = next((i for i, l in enumerate(lines) if "<< 'REMOTE'" in l), None)
    if start is None:
        pytest.fail("Heredoc REMOTE introuvable dans le script de déploiement.")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == "REMOTE"), None)
    if end is None:
        pytest.fail("Fin du heredoc REMOTE introuvable.")
    return "\n".join(lines[start + 1:end])


@pytest.fixture
def remote_script() -> str:
    return _extract_remote_script()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _build_sandbox(base: Path, docker_log: Path, counter: Path, target: int,
                   docker_state: str | None = None) -> Path:
    """Crée un ``bin/`` avec les commandes factices et renvoie son chemin.

    ``docker_state`` permet de simuler l'état renvoyé par
    ``docker inspect -f '{{.State.Health.Status}}'`` : ``healthy`` / ``unhealthy``
    (réutilisé par le script avant le repli curl) ; ``None`` simule un conteneur
    sans état lisible (repli sur ``curl /api/health``).
    """
    bindir = base / "bin"
    bindir.mkdir(parents=True, exist_ok=True)

    # `git clone --depth 1 URL DEST` → recrée `<DEST>/data/.gitkeep` comme le
    # vrai dépôt (seul `data/*` est gitignoré mais `.gitkeep` est suivi).
    _write_executable(bindir / "git", f"""#!/usr/bin/env bash
set -e
if [[ "${{1}}" == "clone" ]]; then
  DEST="${{@: -1}}"
  mkdir -p "$DEST/data"
  touch "$DEST/data/.gitkeep"
  echo "clone $DEST" >> "$DOCKER_LOG"
  exit 0
fi
echo "git: argument inattendu : ${{@}}" >&2
exit 1
""")

    # `docker` :
    #   - `docker inspect -f '{{.State.Health.Status}}' guitarlab` → renvoie l'état
    #     lu depuis `$DOCKER_STATE_FILE` s'il existe, sinon échec (= état inconnu) ;
    #   - `docker compose up -d --build` → no-op qui journalise le répertoire.
    _write_executable(bindir / "docker", f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "inspect" ]]; then
  if [ -f "$DOCKER_STATE_FILE" ] && [ -s "$DOCKER_STATE_FILE" ]; then
    cat "$DOCKER_STATE_FILE"; exit 0
  fi
  exit 1
fi
echo "docker-up $PWD" >> "$DOCKER_LOG"
exit 0
""")

    # `curl -fsS ...` → succès à partir du N-ième appel (compteur partagé).
    _write_executable(bindir / "curl", f"""#!/usr/bin/env bash
n=0
if [ -f "$CURL_COUNTER" ]; then n=$(cat "$CURL_COUNTER"); fi
n=$((n + 1))
echo "$n" > "$CURL_COUNTER"
if [ "$n" -ge "$CURL_TARGET" ]; then exit 0; else exit 1; fi
""")

    # `sleep` → no-op (accélère la boucle de polling).
    _write_executable(bindir / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    if docker_state is not None:
        (base / "docker_state").write_text(docker_state + "\n")

    return bindir


def _run_remote(remote_script: str, home: Path, target: int,
                with_prev: bool = False, docker_state: str | None = None,
                health_timeout: int = 180):
    """Exécute le script distant avec des commandes factices, renvoie un tuple
    ``(code, docker_log, counter, bin, stderr)``."""
    base = Path(tempfile.mkdtemp())
    docker_log = base / "docker.log"
    counter = base / "curl_counter"
    bindir = _build_sandbox(base, docker_log, counter, target, docker_state)

    if with_prev:
        guitarlab = home / "guitarlab"
        (guitarlab / "releases" / "prev-release").mkdir(parents=True, exist_ok=True)
        (guitarlab / "data").mkdir(parents=True, exist_ok=True)
        (guitarlab / "current").symlink_to(guitarlab / "releases" / "prev-release")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["DOCKER_LOG"] = str(docker_log)
    env["CURL_COUNTER"] = str(counter)
    env["CURL_TARGET"] = str(target)
    env["DEPLOY_REPO_URL"] = "https://example.invalid/repo.git"
    # Délai de polling configurable (hérité de la variable de dépôt/org) : fixé
    # ici pour un nombre de tentatives déterministe et rapide dans les tests.
    env["DEPLOY_HEALTH_TIMEOUT_SECS"] = str(health_timeout)
    if docker_state is not None:
        env["DOCKER_STATE_FILE"] = str(base / "docker_state")

    proc = subprocess.run(
        ["bash", "-s"], input=remote_script, text=True, env=env,
        capture_output=True,
    )
    return proc.returncode, docker_log, counter, bindir, proc.stderr


def _current_path(home: Path) -> Path:
    return home / "guitarlab" / "current"


def test_deploy_data_symlink_is_clean_and_not_nested(remote_script):
    """Le ``current/data`` doit être UN symlink vers ``$APP_ROOT/data``, jamais un
    lien imbriqué ``data/data`` : sinon l'app monterait une bibliothèque vide."""
    home = Path(tempfile.mkdtemp())
    rc, log, counter, _, _ = _run_remote(remote_script, home, target=1)
    assert rc == 0
    current = _current_path(home)
    assert current.is_symlink()
    data = current / "data"
    assert data.is_symlink(), "current/data doit être un symlink vers APP_ROOT/data"
    assert os.readlink(data) == str(home / "guitarlab" / "data")
    # Aucun lien imbriqué data/data, aucun .gitkeep résiduel sous le symlink.
    assert not (current / "data" / "data").exists()
    assert (home / "guitarlab" / "data").is_dir()


def test_deploy_no_false_rollback_on_slow_health(remote_script):
    """Le polling attend que /api/health soit prêt (5 appels) sans rollback : la
    boucle remplace le `sleep 20` fixe qui provoquait de faux rollbacks."""
    home = Path(tempfile.mkdtemp())
    rc, log, counter, _, _ = _run_remote(remote_script, home, target=5)
    assert rc == 0
    # La release courante est bien la NOUVELLE (pas de rollback vers un PREV).
    current = _current_path(home)
    assert current.is_symlink()
    assert "prev-release" not in os.readlink(current)
    # Le healthcheck a été interrogé exactement jusqu'à ce qu'il soit prêt.
    assert int(counter.read_text()) == 5


def test_deploy_rolls_back_after_polling_timeout(remote_script):
    """Après ~36 interrogations sans santé, le job fait un rollback sur la release
    précédente et se termine en échec (code 1)."""
    home = Path(tempfile.mkdtemp())
    rc, log, counter, _, _ = _run_remote(remote_script, home, target=999, with_prev=True)
    assert rc == 1
    current = _current_path(home)
    # current doit pointer sur la release précédente (rollback).
    assert current.is_symlink()
    assert os.readlink(current) == str(home / "guitarlab" / "releases" / "prev-release")
    # Le polling a bien été épuisé (36 tentatives) avant la bascule.
    assert int(counter.read_text()) == 36


def test_docker_healthy_state_is_reused_without_curl(remote_script):
    """Le script réutilise l'état Docker ``healthy`` (qui respecte le
    ``start_period`` du healthcheck Compose) : il sort immédiatement du polling
    sans jamais appeler ``curl /api/health`` ni déclencher de rollback."""
    home = Path(tempfile.mkdtemp())
    rc, log, counter, _, _ = _run_remote(
        remote_script, home, target=999, docker_state="healthy")
    assert rc == 0
    current = _current_path(home)
    assert current.is_symlink()
    assert "prev-release" not in os.readlink(current)  # pas de rollback
    # Le repli curl n'a jamais été sollicité (le Docker inspect suffit).
    assert not counter.exists() or int(counter.read_text()) == 0


def test_docker_unhealthy_state_triggers_rollback(remote_script):
    """L'état Docker ``unhealthy`` fait échouer immédiatement (rollback), sans
    attendre le délai complet ni passer par le repli curl."""
    home = Path(tempfile.mkdtemp())
    rc, log, counter, _, _ = _run_remote(
        remote_script, home, target=999, with_prev=True, docker_state="unhealthy")
    assert rc == 1
    current = _current_path(home)
    assert current.is_symlink()
    assert os.readlink(current) == str(home / "guitarlab" / "releases" / "prev-release")
    assert not counter.exists() or int(counter.read_text()) == 0


def test_health_timeout_is_configurable(remote_script):
    """``DEPLOY_HEALTH_TIMEOUT_SECS`` pilote le nombre de tentatives : timeout
    40 s → 40/5 = 8 interrogations avant le rollback (et non un délai figé)."""
    home = Path(tempfile.mkdtemp())
    rc, log, counter, _, _ = _run_remote(
        remote_script, home, target=999, with_prev=True, health_timeout=40)
    assert rc == 1
    assert int(counter.read_text()) == 8


def test_repo_url_with_apostrophe_is_quoting_safe(remote_script):
    """Une URL de dépôt contenant une apostrophe ne casse plus le quoting : elle
    est encodée en base64 côté local et décodée côté distant (transport par
    stdin), puis correctement utilisée par ``git clone``."""
    home = Path(tempfile.mkdtemp())
    base = Path(tempfile.mkdtemp())
    bindir = base / "bin"
    bindir.mkdir(parents=True)
    url_log = base / "clone.log"

    # `git clone --depth 1 URL DEST` → crée la release et journalise les args
    # (dont l'URL décodée) ; les autres commandes lourdes sont neutralisées.
    _write_executable(bindir / "git", f"""#!/usr/bin/env bash
DEST="${{@: -1}}"
mkdir -p "$DEST/data"
touch "$DEST/data/.gitkeep"
echo "$@" >> "$URL_LOG"
exit 0
""")
    _write_executable(bindir / "docker", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bindir / "curl", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bindir / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    repo_url = "https://user:to'ken@example.invalid/repo.git"
    encoded = base64.b64encode(repo_url.encode("utf-8")).decode("ascii")
    stdin_text = (
        f"REPO_URL_B64='{encoded}'\n"
        "DEPLOY_HEALTH_TIMEOUT_SECS=180\n"
        + remote_script
    )

    env = dict(os.environ)
    env.update(
        HOME=str(home),
        PATH=f"{bindir}:{env['PATH']}",
        URL_LOG=str(url_log),
        DEPLOY_REPO_URL="https://sentinel.invalid/ignored.git",  # doit être surchargé
        DEPLOY_HEALTH_TIMEOUT_SECS="180",
    )
    proc = subprocess.run(
        ["bash", "-s"], input=stdin_text, text=True, env=env, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    # L'URL (avec apostrophe) a été décodée puis passée à `git clone`.
    assert repo_url in url_log.read_text()


def test_repo_url_transported_via_stdin_not_command_line():
    """La valeur de ``DEPLOY_REPO_URL`` ne doit plus figurer sur la ligne de
    commande ssh (donc être visible dans ``ps``) : elle est encodée en base64
    et injectée en tête du heredoc envoyé sur stdin à ``bash -s``."""
    run = _local_run_block()
    # Ancienne forme (valeur en clair sur la ligne de commande ssh, cassable par
    # une apostrophe) : elle a disparu.
    assert "DEPLOY_REPO_URL='$DEPLOY_REPO_URL' bash -s" not in run
    # Le transport repose sur un base64 injecté dans le heredoc → stdin (bash -s),
    # jamais sur un argument de la ligne de commande ssh.
    assert "REPO_URL_B64=" in run
    assert "base64" in run
    assert "<< 'REMOTE'" in run
    assert "bash -s" in run


@pytest.mark.parametrize("timeout", [1, 3])
def test_deploy_no_false_rollback_when_timeout_below_step(remote_script, timeout):
    """Un ``DEPLOY_HEALTH_TIMEOUT_SECS`` < 5s (ex. 1 ou 3) ne doit pas produire
    zéro sondage : on garantit au moins une itération (clamp à ≥ 1), donc pas de
    faux rollback immédiat sur un healthcheck sain."""
    home = Path(tempfile.mkdtemp())
    rc, log, counter, _, _ = _run_remote(
        remote_script, home, target=1, health_timeout=timeout)
    assert rc == 0
    current = _current_path(home)
    # Pas de rollback : current pointe sur la NOUVELLE release.
    assert current.is_symlink()
    assert "prev-release" not in os.readlink(current)
    # Le healthcheck a bien été sondé au moins une fois.
    assert int(counter.read_text()) >= 1


def test_base64_decode_falls_back_to_openssl(remote_script):
    """Le décodage base64 distant est portable : si ``base64 -d`` échoue (système
    sans GNU base64, ou implémentation BSD), le repli ``openssl base64 -d`` doit
    décoder l'URL correctement (aucun échec ni URL corrompue)."""
    home = Path(tempfile.mkdtemp())
    base = Path(tempfile.mkdtemp())
    bindir = base / "bin"
    bindir.mkdir(parents=True)
    url_log = base / "clone.log"

    # `base64` est rendu défaillant : le remote doit retomber sur openssl.
    _write_executable(bindir / "base64", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(bindir / "git", f"""#!/usr/bin/env bash
DEST="${{@: -1}}"
mkdir -p "$DEST/data"
touch "$DEST/data/.gitkeep"
echo "$@" >> "$URL_LOG"
exit 0
""")
    _write_executable(bindir / "docker", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bindir / "curl", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bindir / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    repo_url = "https://example.invalid/repo.git"
    encoded = base64.b64encode(repo_url.encode("utf-8")).decode("ascii")
    stdin_text = (
        f"REPO_URL_B64='{encoded}'\n"
        "DEPLOY_HEALTH_TIMEOUT_SECS=180\n"
        + remote_script
    )

    env = dict(os.environ)
    env.update(
        HOME=str(home),
        PATH=f"{bindir}:{env['PATH']}",
        URL_LOG=str(url_log),
        DEPLOY_REPO_URL="https://sentinel.invalid/ignored.git",  # doit être surchargé
        DEPLOY_HEALTH_TIMEOUT_SECS="180",
    )
    proc = subprocess.run(
        ["bash", "-s"], input=stdin_text, text=True, env=env, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    # L'URL a été décodée via le repli openssl puis passée à `git clone`.
    assert repo_url in url_log.read_text()
