r"""Tests MISSION : sécurité et robustesse du job de déploiement CI/CD.

Le déploiement (``.github/workflows/ci.yml``) a été durci : secrets GitHub
(uniquement, jamais de valeur en clair), utilisateur **non root**, clé hôte
épinglée par empreinte SHA256, ``StrictHostKeyChecking=yes``, bascule atomique
du symlink ``current`` avec **rollback** sur healthcheck en échec, suppression
du ``reset --hard``.

Ces invariants sont vérifiés ici par lecture statique du workflow : impossible
d'exécuter un SSH/Docker réel dans l'environnement de test, on s'assure donc
que le fichier respecte les propriétés attendues (structure + script). La
couverture du moteur de mastering (stems vides, NaN/±inf) reste dans
``tests/test_mastering.py``.
"""
from pathlib import Path

import pytest

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

# Secrets attendus (jamais en clair dans le dépôt).
REQUIRED_SECRETS = {
    "DEPLOY_HOST",
    "DEPLOY_USER",
    "DEPLOY_SSH_PRIVATE_KEY",
    "DEPLOY_HOST_KEY_FINGERPRINT",
    "DEPLOY_REPO_URL",
}

# Jetons de sécurité interdits dans le script de déploiement.
FORBIDDEN_STRINGS = [
    "reset --hard",           # reproprimer une release active
    "StrictHostKeyChecking=no",
    "StrictHostKeyChecking=off",
    "root@",                  # connexion root interdite
]


def _collect_secrets(content: str) -> set[str]:
    """Valeurs référencées via ``secrets.NAME`` dans un bloc du workflow."""
    import re
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", content))


def _strip_comments(script: str) -> str:
    """Retire les lignes de commentaire shell (on juge le script réellement
    exécuté, pas les commentaires d'explication du workflow)."""
    return "\n".join(
        line for line in script.splitlines()
        if not line.lstrip().startswith("#")
    )


@pytest.fixture
def workflow() -> dict:
    """Charge et valide la syntaxe YAML du workflow de déploiement."""
    if not WORKFLOW.exists():
        pytest.fail(f"Workflow introuvable : {WORKFLOW}")
    with open(WORKFLOW, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for job in ("test", "deploy"):
        assert job in data.get("jobs", {}), f"Job « {job} » manquant."
    return data


@pytest.fixture
def deploy(workflow) -> dict:
    """Le job ``deploy`` : sa structure + le script réel (commentaires ôtés)."""
    job = workflow["jobs"]["deploy"]
    script = "\n".join(
        s.get("run", "") for s in job.get("steps", []) if "run" in s
    )
    return {"job": job, "script": _strip_comments(script)}


# --------------------------------------------------------------------------- #
# Structure du job
# --------------------------------------------------------------------------- #
def test_deploy_gated_on_tests(workflow):
    assert workflow["jobs"]["deploy"].get("needs") == "test"


def test_deploy_uses_production_environment(workflow):
    assert workflow["jobs"]["deploy"].get("environment") == "production"


def test_deploy_only_on_master_and_not_pr(workflow):
    deploy_if = workflow["jobs"]["deploy"].get("if", "")
    assert "refs/heads/master" in deploy_if
    # Le déploiement est explicitement désactivé sur les pull requests.
    assert "github.event_name != 'pull_request'" in deploy_if


# --------------------------------------------------------------------------- #
# Secrets : aucune valeur en clair, tous référencés via secrets.
# --------------------------------------------------------------------------- #
def test_deploy_uses_only_github_secrets(deploy):
    referenced = _collect_secrets(yaml.safe_dump(deploy["job"]))
    assert REQUIRED_SECRETS <= referenced, (
        f"Secrets manquants : {REQUIRED_SECRETS - referenced}")


def test_no_hardcoded_host_root_or_private_key(deploy):
    """Aucune IP en clair, aucun ``root@``, aucune clé privée dans le workflow."""
    script = deploy["script"]
    import re
    # IPv4 en clair (le port 127.0.0.1 du healthcheck est légitime, on l'autorise).
    ips = set(re.findall(r"\b(?!127\.0\.0\.1)(?:\d{1,3}\.){3}\d{1,3}\b", script))
    assert not ips, f"Adresse IP en clair détectée : {ips}"
    assert "root@" not in script
    # Pas de bloc de clé privée collé en dur (indicateurs PEM/OpenSSH).
    for marker in ("-----BEGIN", "ssh-ed25519 AAAA", "ssh-rsa AAAA"):
        assert marker not in script, f"Matériau de clé privée en clair : {marker}"


# --------------------------------------------------------------------------- #
# Épinglage de la clé hôte + transport sécurisé
# --------------------------------------------------------------------------- #
def test_host_key_pinned_and_verified(deploy):
    script = deploy["script"]
    assert "ssh-keyscan" in script
    assert "ssh-keygen -lf" in script
    assert "DEPLOY_HOST_KEY_FINGERPRINT" in script


def test_strict_host_key_checking_enabled(deploy):
    script = deploy["script"]
    assert "StrictHostKeyChecking=yes" in script
    assert "StrictHostKeyChecking=no" not in script


def test_forbidden_destructive_patterns_absent(deploy):
    script = deploy["script"]
    for token in FORBIDDEN_STRINGS:
        assert token not in script, f"Motif interdit présent : {token}"


# --------------------------------------------------------------------------- #
# Déploiement atomique par symlink + rollback
# --------------------------------------------------------------------------- #
def test_atomic_symlink_switch_to_current(deploy):
    script = deploy["script"]
    assert 'ln -sfn "$RELEASE" "$CURRENT"' in script


def test_rollback_on_failed_healthcheck(deploy):
    script = deploy["script"]
    assert "rollback" in script.lower()
    assert "PREV" in script
    assert 'ln -sfn "$PREV" "$CURRENT"' in script
    assert '/api/health' in script


def test_healthcheck_endpoint_used(deploy):
    script = deploy["script"]
    assert "curl" in script and "/api/health" in script


def test_old_releases_purged(deploy):
    script = deploy["script"]
    assert "rm -rf" in script
    assert "tail -n +5" in script  # conserve la courante + 3 précédentes


def test_ssh_key_permissions_hardened(deploy):
    script = deploy["script"]
    assert "chmod 700" in script
    assert "chmod 600" in script
    assert "umask 077" in script


# --------------------------------------------------------------------------- #
# Correctif MISSION : symlink data (éviter le lien imbriqué data/data)
# --------------------------------------------------------------------------- #
def test_data_symlink_created_after_removing_release_data(deploy):
    """La suppression de ``$RELEASE/data`` (recréé par le clone via
    ``data/.gitkeep``) doit précéder le ``ln -sfn`` vers la bibliothèque :
    sinon un symlink imbriqué ``$RELEASE/data/data`` est créé et l'application
    monte une bibliothèque vide."""
    lines = deploy["script"].splitlines()

    def _index(predicate):
        hits = [i for i, l in enumerate(lines) if predicate(l)]
        assert hits, "Ligne attendue absente du script."
        return hits[0]

    rm_idx = _index(lambda l: 'rm -rf "$RELEASE/data"' in l)
    ln_idx = _index(lambda l: 'ln -sfn "$APP_ROOT/data" "$RELEASE/data"' in l)
    assert rm_idx < ln_idx, (
        "Le `rm -rf $RELEASE/data` doit venir AVANT le `ln -sfn`.")


def test_data_symlink_targets_persistent_library(deploy):
    script = deploy["script"]
    assert 'ln -sfn "$APP_ROOT/data" "$RELEASE/data"' in script
    # Aucun lien imbriqué ne doit subsister dans le scénario corrigé.
    assert '"$RELEASE/data/data"' not in script


# --------------------------------------------------------------------------- #
# Correctif MISSION : poll /api/health au lieu d'un sleep fixe
# --------------------------------------------------------------------------- #
def test_healthcheck_now_polls_instead_of_fixed_sleep(deploy):
    """Le `sleep 20` fixe (faux rollbacks liés au boot torch/demucs et au
    `start_period` de 30s) est remplacé par une boucle de polling dont le délai
    maximal est configurable (défaut 300s)."""
    script = deploy["script"]
    assert "sleep 20" not in script
    assert "sleep 5" in script
    assert "HEALTH_OK" in script
    # Le nombre de tentatives découle d'un délai configurable, plus un simple
    # compteur codé en dur (sinon un démarrage > 180s déclencherait un faux
    # rollback).
    assert 'for _ in $(seq 1 "$HEALTH_ATTEMPTS")' in script
    assert "HEALTH_TIMEOUT_SECS" in script
    assert "DEPLOY_HEALTH_TIMEOUT_SECS" in script


def test_healthcheck_poll_gates_the_rollback(deploy):
    """La décision de rollback n'intervient qu'après l'échec du polling complet."""
    script = deploy["script"]
    assert 'if [ "$HEALTH_OK" -ne 1 ]; then' in script
    assert 'ln -sfn "$PREV" "$CURRENT"' in script


def test_healthcheck_reuses_docker_state_with_curl_fallback(deploy):
    """Le polling réutilise l'état Docker ``healthy``/``unhealthy`` (qui respecte
    le ``start_period`` de 30s) et ne se rabat sur ``curl /api/health`` qu'en
    l'absence d'état exploitable."""
    script = deploy["script"]
    assert "docker inspect" in script
    assert "'{{.State.Health.Status}}'" in script
    assert '"healthy"' in script
    assert '"unhealthy"' in script
    assert "curl -fsS" in script
    assert 'http://127.0.0.1:8005/api/health' in script


def test_health_timeout_default_is_configurable(workflow):
    """Le délai de polling est injecté depuis une variable de dépôt/org
    (``vars.DEPLOY_HEALTH_TIMEOUT_SECS``) avec un repli à 300 s : pas de valeur
    codée en dur dans le job."""
    step = workflow["jobs"]["deploy"]["steps"][1]
    env = step.get("env", {})
    assert "DEPLOY_HEALTH_TIMEOUT_SECS" in env
    value = env["DEPLOY_HEALTH_TIMEOUT_SECS"]
    assert "vars.DEPLOY_HEALTH_TIMEOUT_SECS" in value
    assert "'300'" in value


def test_repo_url_carried_via_base64_not_on_ssh_command_line(deploy, workflow):
    """L'URL du dépôt ne doit plus figurer sur la ligne de commande ``ssh``
    (visibilité ``ps``) : elle est encodée en base64 et injectée via stdin."""
    script = deploy["script"]
    # L'ancienne forme embarquait la valeur sur la ligne de commande ssh.
    assert "DEPLOY_REPO_URL='$DEPLOY_REPO_URL' bash -s" not in script
    assert "REPO_URL_B64=" in script
    assert "base64 -w0" in script
    assert "base64 -d" in script
    # La commande ssh s'étend sur plusieurs lignes (continuation `\`) : on la
    # reconstruit jusqu'au `bash -s` pour vérifier qu'aucun secret ne transite.
    ssh_block, capturing = [], False
    for line in script.splitlines():
        if line.strip().startswith("ssh "):
            capturing = True
        if capturing:
            ssh_block.append(line)
            if "bash -s" in line:
                break
    joined = "\n".join(ssh_block)
    assert joined, "Ligne de commande ssh absente."
    assert "$DEPLOY_REPO_URL" not in joined
    assert "bash -s" in joined


# --------------------------------------------------------------------------- #
# Correctif MISSION : clamp HEALTH_ATTEMPTS ≥ 1 + décodage base64 portable
# --------------------------------------------------------------------------- #
def test_health_attempts_clamped_to_at_least_one(deploy):
    """Le nombre de sondages ne peut jamais tomber à 0 : division plafonnée
    ``(SECS + 4) / 5`` + borne basse explicite, pour qu'un ``timeout < 5s`` ne
    déclenche pas de faux rollback immédiat."""
    script = deploy["script"]
    assert "(HEALTH_TIMEOUT_SECS + 4) / 5" in script
    assert 'if [ "$HEALTH_ATTEMPTS" -lt 1 ]; then' in script
    assert "HEALTH_ATTEMPTS=1" in script


def test_base64_decode_is_portable(deploy):
    """Le décodage distant retombe sur plusieurs implémentations : ``base64 -d``
    (GNU), ``base64 -D`` (BSD/macOS), puis ``openssl base64 -d`` (avec saut de
    ligne) — cohérent avec l'encodage local multiplateforme."""
    script = deploy["script"]
    assert "_b64_decode()" in script
    assert "base64 -d 2>/dev/null" in script
    assert "base64 -D 2>/dev/null" in script
    assert "openssl base64 -d 2>/dev/null" in script
    # Le repli openssl ajoute un saut de ligne (nécessaire au décodeur OpenSSL).
    assert "printf '%s\\n' \"$1\" | openssl base64 -d" in script
