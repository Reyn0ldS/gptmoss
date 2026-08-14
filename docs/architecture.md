# Architecture de GPTMOSS

Cette cartographie décrit le système réellement versionné. Son inventaire fonctionnel est
`docs/application-map.json` et son [graphe relationnel](symbol-relations.md) est
`docs/symbol-map.json`. Leur cohérence est contrôlée par
`scripts/validate_application_map.py`. Toute évolution des surfaces publiques doit modifier
le code, les cartes et les tests dans le même commit.

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
outils, les validations et la finalisation. `Scheduler` est le service temporel unique du
processus : démarrage planifié, délais de reprise, attente fournisseur et reprise après
redémarrage y convergent. Sa file est reconstruite depuis l'état persistant ; ce n'est
pas un ordonnanceur distribué ni une implémentation de cron.

## Responsabilités par couche

| Couche | Modules propriétaires | Responsabilité |
|---|---|---|
| Entrée et bootstrap | `main.py` | Charge et normalise la configuration, construit les fournisseurs, registres, moteur et capacités, lance CLI ou Uvicorn. |
| Contrôle du processus | `scripts/server_supervisor.py` | Conserve un point de contrôle local lorsque l'application est arrêtée ; vérifie port, santé et jeton éphémère. |
| Interface | `gptmoss/api/gui.html` | Composition des tâches, suivi temps réel, bibliothèque, mémoire, skills, réglages, livraisons et contrôle serveur. |
| API | `gptmoss/api/server.py` | Contrats HTTP/WebSocket, validation Pydantic, cycle de vie, réglages, diagnostics et téléchargements bornés. |
| Orchestration | `core/kernel.py`, `core/execution.py`, `core/execution_plan.py`, `core/execution_progress.py`, `core/execution_rescue.py`, `core/workload.py`, `core/plan_obligations.py`, `core/scheduler.py`, `planners/simple.py`, `planners/complexity.py`, `planners/fallbacks.py` | Création/exécution planifiée, plan sémantique sans cardinalité fixe, contrats d'opération/preuve, réparation causale, compilation selon le volume réel, dépendances, spécialistes, reprises, approbations et convergence. |
| Domaines projet | `core/domains.py` | Catégories génériques et extensions de marqueurs propres à chaque projet, sans hypothèse métier globale. |
| Contexte et mémoire | `core/context.py`, `memory/json_store.py`, `capabilities/memory.py` | Contexte borné et mémoire gouvernée par projet, validation, provenance, TTL, déduplication et supersession. |
| Capacités | `capabilities/*` | Actions outillées exposées au modèle et contrôlées par la politique. |
| Documents | `core/documents.py`, `core/artifacts.py`, `core/corpus_policy.py`, `capabilities/documents.py` | Détection sûre, manifestes de corpus par dossier, politique locale en lecture seule, déduplication SHA-256, normalisation, indexation, inventaire, recherche et lecture locale des pièces jointes. |
| Qualité et livraison | `core/delivery.py`, `document_quality.py`, `professional_delivery.py`, `delivery_package.py` | Contrat gelé, preuves indépendantes, réparations et paquet professionnel DOCX/ZIP signé par empreintes. |
| Évolution | `core/skills.py`, `core/evolution.py` | Découverte de procédures, profils de spécialistes et évolution locale traçable. |
| Configuration | `core/settings.py` | Contrat Pydantic unique partagé par bootstrap, API, GUI et tests ; valeurs sûres et limites strictes. |
| Persistance | `core/state.py`, `core/durable_io.py`, `core/observability.py` | Index `state_store.json` plus sidecars par exécution/conversation, migration/quarantaine, historique borné et télémétrie locale. |
| Fournisseur | `providers/qwen.py`, `providers/qwen_support.py` | Adaptateur OpenAI-compatible, TLS sûr, vision, préflight jetons/outils, réserve de sortie, apprentissage de la fenêtre réelle, compactage et reprise. |

Les interfaces abstraites de `gptmoss/interfaces/` séparent capacités, LLM, mémoire,
planification et politique de leurs implémentations actuelles.

## Capacités agentiques

| Capacité | Actions | Frontière principale |
|---|---|---|
| `filesystem` | `read`, `write`, `append`, `list_dir`, `delete` | Résolution dans le workspace de l'exécution ; écriture incrémentale des grands artefacts sur leur chemin déclaré ; sous-dossiers et suppression configurables. |
| `documents` | `inventory`, `search`, `read`, `read_chunk`, `read_image`, `read_images` | Pièces explicitement jointes uniquement ; texte normalisé et images sélectionnées par lots multimodaux bornés. |

Les quality gates pilotent aussi le protocole d'outils. Si un rédacteur doit
réparer un document long déjà créé, l'itération suivante ne reçoit que le schéma
`filesystem__append` et impose un appel ; pour un artefact absent, le même mécanisme
emploie `filesystem__write`. Le filtrage est temporaire et levé après la mutation,
ce qui garantit un progrès durable sans figer le nombre d'itérations du plan.
Quand le serveur compatible OpenAI utilise le protocole textuel de secours, le
nom de l'unique outil obligatoire est répété dans le dernier message transmis ;
il ne peut ainsi être masqué par un long historique de réparations.
Un défaut qui exige de retirer du contenu (doublon, référence invalide, lien externe
ou placeholder) bascule explicitement vers une reconstruction incrémentale : un
premier `filesystem__write` borné initialise la version propre, puis les tours
suivants emploient `filesystem__append`.
La politique de mutation interdit également la suppression, directe ou via le
shell, d'un artefact obligatoire de l'étape active, ainsi que son écrasement par
un contenu vide. Une correction destructive doit employer une écriture contrôlée,
afin qu'un livrable ne disparaisse jamais entre deux passages des quality gates.
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

Avant de modifier un symbole structurant, exécuter
`python scripts/analyze_impact.py <symbole>` afin d'identifier ses appelants, données,
surfaces publiques et tests. Le graphe doit ensuite être régénéré avec
`python scripts/generate_symbol_map.py`.

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

## Services de fiabilité

Les responsabilités transversales ont des propriétaires explicites : `Scheduler` pour
toute échéance, `ProviderRecoveryCoordinator` pour la décision de reprise LLM,
`DeliveryCoordinator` pour l'assurance, `ApprovalCoordinator` pour les décisions
humaines, `ContextWindowPolicy` et `ToolCallParser` pour Qwen, puis `ShellSafetyPolicy`,
`ProcessRegistry` et `ProcessRunner` pour le shell. `ExecutionEngine` reste la façade
compatible ; la coordination du plan, l'exécution d'une étape et le traitement d'un lot
d'outils sont désormais des méthodes séparées et testées.

`ExecutionEngine.start_execution` possède un registre indexé par identifiant d'exécution.
Les reprises API, approbations, sous-agents et jobs du `Scheduler` convergent vers ce
point unique. `cancel_active_execution` retire aussi les jobs de reprise fournisseur et
annule la coroutine possédée ; son `finally` annule les tâches d'étapes encore actives.
L'arrêt du runtime vide ce registre avant de fermer le transport du fournisseur.

`ExecutionState.status` est validé par `ExecutionStatus`. Les mutations du runtime passent
par `StateEngine.transition_execution`, qui contrôle la transition et conserve son motif,
son acteur, sa corrélation et son horodatage. La persistance v3 référence des sidecars
immuables par identité et SHA-256 : seules les générations modifiées sont produites et un
index atomique constitue le point de commit. L'arrêt FastAPI effectue un flush final puis
retire son abonnement au bus.

## Dette et limites explicites

- La file du `Scheduler` vit en mémoire ; les échéances importantes sont persistées dans
  l'exécution puis réinscrites au bootstrap, mais plusieurs processus ne partagent pas
  encore une file distribuée.
- Le PDF extrait le texte local avec `pypdf` ; l'OCR des pages image n'est pas implémenté.
- La GUI est volontairement un fichier HTML/JS autonome, ce qui simplifie le paquet
  offline mais concentre une grande surface dans un seul fichier.
- Le contrôle serveur complet est fourni par le lanceur supervisé Windows ; `main.py`
  lancé seul ne peut évidemment pas redémarrer son propre processus après arrêt.
