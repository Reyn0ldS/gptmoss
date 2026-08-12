# Architecture de GPTMOSS

Cette cartographie décrit le système réellement versionné. Son inventaire machine est
`docs/application-map.json` et sa cohérence est contrôlée par
`scripts/validate_application_map.py`. Toute évolution des surfaces publiques doit modifier
le code, la carte et les tests dans le même commit.

## Vue d'ensemble

```text
start.bat / start.sh / main.py --task
        |
        +-- superviseur local :8765 (Windows)
        |       `-- démarre, arrête, redémarre et rebinde main.py
        |
        `-- main.py :8000
                +-- GUI HTML/JS
                +-- API FastAPI + WebSockets
                +-- RuntimeKernel
                       +-- ExecutionEngine
                       |      +-- Planner + Context + Skills
                       |      +-- Policy + capacités contrôlées
                       |      +-- Assurance + paquet de livraison
                       +-- StateEngine + mémoire + télémétrie
                       `-- EventBus -> API/WebSockets/GUI
```

Le serveur HTTP reste l'unique façade applicative. Le noyau crée les exécutions et le
moteur en assure la planification, l'ordonnancement par dépendances, les appels LLM et
outils, les validations et la finalisation. `Scheduler` est aujourd'hui une réserve
d'architecture : il n'est pas branché au runtime et ne faut donc pas lui attribuer une
fonction de cron ou de file persistante.

## Responsabilités par couche

| Couche | Modules propriétaires | Responsabilité |
|---|---|---|
| Entrée et bootstrap | `main.py` | Charge et normalise la configuration, construit les fournisseurs, registres, moteur et capacités, lance CLI ou Uvicorn. |
| Contrôle du processus | `scripts/server_supervisor.py` | Conserve un point de contrôle local lorsque l'application est arrêtée ; vérifie port, santé et jeton éphémère. |
| Interface | `gptmoss/api/gui.html` | Composition des tâches, suivi temps réel, bibliothèque, mémoire, skills, réglages, livraisons et contrôle serveur. |
| API | `gptmoss/api/server.py` | Contrats HTTP/WebSocket, validation Pydantic, cycle de vie, réglages, diagnostics et téléchargements bornés. |
| Orchestration | `core/kernel.py`, `core/execution.py`, `planners/simple.py` | Création d'exécution, plan adaptatif, dépendances, spécialistes, reprises, approbations et convergence. |
| Contexte et mémoire | `core/context.py`, `memory/json_store.py`, `capabilities/memory.py` | Contexte borné et mémoire gouvernée par projet, validation, provenance, TTL, déduplication et supersession. |
| Capacités | `capabilities/*` | Actions outillées exposées au modèle et contrôlées par la politique. |
| Documents | `core/documents.py`, `core/artifacts.py`, `capabilities/documents.py` | Détection sûre, normalisation, indexation, inventaire, recherche et lecture locale des pièces jointes. |
| Qualité et livraison | `core/delivery.py`, `document_quality.py`, `professional_delivery.py`, `delivery_package.py` | Contrat gelé, preuves indépendantes, réparations et paquet professionnel DOCX/ZIP signé par empreintes. |
| Évolution | `core/skills.py`, `core/evolution.py` | Découverte de procédures, profils de spécialistes et évolution locale traçable. |
| Persistance | `core/state.py`, `core/durable_io.py`, `core/observability.py` | Écritures durables, reprise d'état et télémétrie locale. |
| Fournisseur | `providers/qwen.py` | Adaptateur OpenAI-compatible, TLS, vision et classification/reprise d'erreurs. |

Les interfaces abstraites de `gptmoss/interfaces/` séparent capacités, LLM, mémoire,
planification et politique de leurs implémentations actuelles.

## Capacités agentiques

| Capacité | Actions | Frontière principale |
|---|---|---|
| `filesystem` | `read`, `write`, `list_dir`, `delete` | Résolution dans le workspace de l'exécution ; sous-dossiers et suppression configurables. |
| `documents` | `inventory`, `search`, `read`, `read_chunk` | Pièces explicitement jointes uniquement ; formats locaux normalisés. |
| `memory` | `search`, `propose` | Lecture validée du projet ; une proposition agent reste non validée et non globale. |
| `shell` | `execute` | Répertoire du projet, blocage destructif, timeout, sortie bornée et approbation selon politique. |
| `agent` | `spawn`, `status`, `execute_subtask` | Lignée et profondeur de délégation, refus des cycles exacts. |
| `devteam` | `approve_quality_gate`, `build_project` | Pipeline logiciel spécialisé et gate humain avant livraison. |

Le décorateur de chaque capacité constitue la source de vérité. Le validateur compare
automatiquement noms et actions au manifeste.

## Frontières de confiance

1. La GUI est un client local non privilégié : les entrées sont validées par l'API.
2. Le fournisseur LLM est externe au processus et ne reçoit que le contexte compilé.
3. Les sorties du modèle ne sont jamais une autorisation : la politique précède chaque
   appel d'outil et les capacités réappliquent leurs propres frontières.
4. Les pièces jointes sont des données non fiables. Les archives OOXML sont bornées et
   les ressources externes ne sont pas suivies.
5. Les chemins projet et livraison sont résolus puis vérifiés avant lecture/écriture.
6. Les réglages sensibles exigent confirmation, et la révélation du secret est limitée à
   la boucle locale et auditée.
7. Le superviseur écoute en boucle locale et exige son jeton éphémère pour toute mutation.

## Persistance et propriété des données

La racine opérationnelle est `workspace/`, jamais le dépôt source. Les fichiers majeurs
sont inventoriés dans `application-map.json` : configuration, état, mémoire, télémétrie,
audit, uploads, profils, skills et événements d'évolution. Les projets standards vivent
dans `workspace/projects/<project_id>` ; un chemin personnalisé doit rester une décision
explicite de l'utilisateur. Les paquets finaux vivent dans
`<projet>/.gptmoss/deliveries/<execution_id>`.

Les écritures d'état et de mémoire sont sérialisées et atomiques. Les suppressions et
les modifications shell hors périmètre ne deviennent pas sûres du seul fait qu'elles
sont demandées par le modèle.

## Règles d'évolution

- Une nouvelle route doit figurer dans la carte, avoir un consommateur ou une raison API,
  et être couverte par un test.
- Une nouvelle capacité/action doit être enregistrée au bootstrap, documentée avec sa
  frontière, et ajoutée aux règles de politique pertinentes.
- Une nouvelle option doit exister dans le modèle API, le template, le bootstrap, la GUI
  si elle est destinée à l'utilisateur, et un test de persistance/application.
- Un nouveau format documentaire doit couvrir détection, limites, provenance, index,
  erreurs et cas de fichier trompeur.
- Une nouvelle dépendance d'exécution doit être épinglée dans exigences et contraintes,
  embarquée, importée en qualification et reflétée par le manifeste offline.
- Un nouveau livrable doit être inclus dans le contrat, l'assurance indépendante, le
  manifeste SHA-256 et le parcours de téléchargement.
- Un nouveau test doit être rattaché à une fonctionnalité de la matrice ; un module ou
  script opérationnel non classé fait échouer la validation.

## Dette et limites explicites

- `core/scheduler.py` est un stub non connecté ; la chronologie courante repose sur les
  dépendances du plan et les tâches `asyncio`, pas sur un ordonnanceur persistant.
- Le PDF extrait le texte local avec `pypdf` ; l'OCR des pages image n'est pas implémenté.
- La GUI est volontairement un fichier HTML/JS autonome, ce qui simplifie le paquet
  offline mais concentre une grande surface dans un seul fichier.
- Le contrôle serveur complet est fourni par le lanceur supervisé Windows ; `main.py`
  lancé seul ne peut évidemment pas redémarrer son propre processus après arrêt.
