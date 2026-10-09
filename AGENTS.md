# RÈGLES STRICTES DE DÉVELOPPEMENT & SUPERVISION

## 1. EXCLUSIVITÉ GIT
- **SEUL L'UTILISATEUR TOUCHE À GIT.**
- L'agent ne doit JAMAIS exécuter de commande git de mutation (commit, push, merge, checkout, rebase, etc.).
- Ne JAMAIS inclure de commandes Git (`git add`, `git commit`, `git push`, etc.) dans les prompts ou missions générés pour l'agent dev.

## 2. EXCLUSIVITÉ DES TESTS
- **L'AGENT DÉVELOPPEUR NE FAIT PAS LES TESTS.**
- Ne JAMAIS demander à l'agent développeur d'exécuter des tests (`pytest`, `npm test`, `cargo test`, etc.).
- L'agent dev se concentre à 100% sur l'implémentation du code.
- L'exécution des tests est la responsabilité exclusive de l'utilisateur.

## 3. FORMAT DES MISSIONS POUR L'AGENT DEV
- Titre clair et objectif concis.
- Tâches ciblées par fichier avec consignes techniques précises.
- ZÉRO section Git.
- ZÉRO consigne de test pour le dev.
