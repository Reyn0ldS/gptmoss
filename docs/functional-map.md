# Cartographie fonctionnelle et chronologique

Ce document suit les parcours utilisateur jusqu'à leurs effets persistants. Les états
d'exécution officiels sont `pending`, `running`, `paused`, `waiting_provider`,
`cancelled`, `completed` et `failed`.

## Démarrage et contrôle du serveur

```text
start.bat
  -> find_python.bat
  -> vérification des imports (ou install.bat)
  -> server_supervisor.py :8765
  -> contrôle du port cible
  -> main.py --host/--port
  -> bootstrap_runtime
  -> /health puis /readiness
  -> GUI connectée + WebSocket global
```

Le superviseur est le parent du serveur. `start`, `stop`, `restart` et `rebind` retournent
l'état réel (`starting`, `running`, `stopped`, `error`) avec PID, port, disponibilité et
dernière erreur. La GUI découvre ce canal par `/api/runtime-control`, puis interroge le
superviseur avec son jeton. Un port occupé est signalé avant de démarrer l'enfant.

## Soumission d'une tâche

1. L'utilisateur choisit un projet, décrit le résultat attendu, sélectionne skills et
   pièces jointes, puis la GUI appelle `POST /executions`.
2. L'API vérifie projet et artefacts, constitue les variables et appelle
   `RuntimeKernel.submit_task`.
3. Le noyau crée l'identifiant, refuse les cycles de délégation, initialise l'état
   `pending`, persiste `scheduled_for`, publie `TaskCreated`/`TaskScheduled` et inscrit
   l'exécution dans le `Scheduler` partagé. `delay_seconds` ou `run_at` permettent une
   échéance future ; sans délai, le même service la lance immédiatement.
4. Le moteur verrouille l'exécution, sélectionne les skills, calcule les capacités et
   passe à `running` avec `ExecutionStarted`.
5. Le contexte rassemble conversation, état, mémoire validée du projet, schémas d'outils
   et pièces jointes bornées ; `ContextBuilt` est publié.
6. Le planner produit exigences, interfaces, dépendances, validations et opérations sans
   quota d'étapes. Il doit seulement couvrir les obligations sémantiques du livrable
   (inventaire source, implémentation, validation indépendante, réparation, audit). Un
   travail de 24 h peut donc produire des dizaines d'étapes. Le texte utilisateur n'est
   jamais réécrit : le workflow corpus est un booléen `corpus_auto_workflow`. Le plan est
   normalisé, les exigences héritées sont fusionnées, puis `workload.py` compile le graphe
   selon les métriques réelles. Un gros corpus est distribué en autant de partitions que
   la charge le justifie, rejoint par une consolidation ; une petite tâche conserve son
   graphe minimal.
7. Une réduction de périmètre requiert `ScopeApprovalRequested` avant toute exécution.

## Ordonnancement et exécution d'une étape

```text
dépendances terminées
  -> StepStarted
  -> spécialisation/skills/contextualisation
  -> boucle LLM
       -> texte final provisoire, ou appel d'outil
       -> contrôle policy : deny | approval | allow
       -> exécution de la capacité
       -> ToolCompleted + nouvelle preuve
  -> vérification artefacts/interfaces/commandes/qualité
       -> réparation/reprise si amélioration possible
       -> StepCompleted ou StepFailed
```

Le plan est la source de l'ordre : une étape n'est exécutable qu'après ses dépendances.
Les prérequis validés sont réutilisés, et les chemins possédés empêchent les spécialistes
concurrents de modifier le même livrable. Une répétition sans preuve consomme le budget de
stagnation ; une modification durable ou une nouvelle validation réussie le remet à zéro.

Toutes les mutations applicatives de statut passent par la table de transitions du
`StateEngine`. Chaque changement conserve ancien et nouvel état, motif, acteur,
corrélation et horodatage. Une transition terminale incohérente est refusée avant
persistance ou publication d'événement.

Les appels shell peuvent mettre l'exécution en `paused` avec `ApprovalRequested`.
L'approbation reprend exactement l'appel en attente ; le rejet devient une preuve à
intégrer au raisonnement. Pause utilisateur, annulation et reprise sont propagées aux
sous-agents compatibles.

## Fournisseur indisponible et reprise

Une erreur transitoire passe l'état à `waiting_provider`, préserve plan, conversation et
résultats, publie `ExecutionWaitingProvider` et programme une nouvelle tentative dans le
`Scheduler` partagé. Les backoffs internes utilisent eux aussi ce service plutôt que des
temporisations indépendantes. Une
erreur d'authentification ou de configuration permanente devient `failed` avec un
diagnostic exploitable : elle ne doit pas boucler comme une panne réseau.

Avant chaque appel, le fournisseur réserve des jetons de sortie, estime messages et
schémas d'outils, puis compacte sous la fenêtre configurée ou apprise. Une erreur donnant
une limite exacte (par exemple 262144 jetons) met à jour l'enveloppe et déclenche une
nouvelle tentative bornée sans perdre l'état durable de l'exécution.

Au redémarrage, les exécutions interrompues, planifiées et celles en attente fournisseur
sont normalisées puis réinscrites sans dupliquer la tâche initiale. Le verrou par
exécution empêche deux boucles simultanées.

## Documents et corpus local

1. La GUI accepte des fichiers isolés ou un dossier récursif (`webkitdirectory`). La case
   d'inventaire automatique envoie `corpus_auto_workflow` sans modifier le texte de la
   tâche. Pour un dossier, `POST /corpora` crée/reprend un manifeste durable ; chaque
   source passe en binaire par `PUT /corpora/{id}/files`, avec chemin relatif et empreinte
   SHA-256.
2. Le navigateur filtre les répertoires techniques et limite l'import à trois fichiers
   simultanés. Le serveur revalide chemin, type, taille, signature et empreinte, puis
   déduplique un contenu déjà importé sous le même chemin logique.
3. `POST /corpora/{id}/finalize` enregistre l'instantané courant, les exclusions et les
   erreurs. La reprise ne renvoie que les fichiers modifiés ; un manifeste partiel reste
   explicite et auditable.
4. `POST /artifacts` décode et stocke un fichier isolé avec un nom sûr.
5. La signature effective détermine le parseur, pas seulement l'extension annoncée.
6. TXT/Markdown/CSV/JSON/XML/HTML, DOCX, PPTX et PDF sont normalisés en blocs avec
   provenance ; les archives sont soumises aux limites de taille et de ratio.
7. L'index documentaire est reconstruit/synchronisé puis devient interrogeable.
8. L'exécution ne voit que les artefacts explicitement attachés. La capacité `documents`
   pagine l'inventaire, recherche/lit le texte par chunks et charge les images ciblées par
   lots bornés via `read_image`/`read_images`.
9. Les citations internes se fondent sur les identifiants, fichiers et positions locales ;
   aucune preuve Internet n'est fabriquée pour un mode corpus local.

Un upload invalide renvoie une erreur client ; une indisponibilité de stockage renvoie un
diagnostic temporaire. Les écritures utilisent des chemins compatibles UNC et un nom de
métadonnées distinct du chemin utilisateur.

## Mémoire gouvernée

- `memory.search` expose par défaut uniquement les éléments validés, actifs et du projet.
- `memory.propose` crée une proposition de projet non validée avec provenance d'exécution.
- La GUI permet création, édition, validation et suppression ; le scope global est une
  décision humaine explicite.
- TTL, déduplication et supersession empêchent les entrées expirées ou remplacées de
  contaminer le contexte courant.

La mémoire conserve des préférences et faits réutilisables ; l'état d'exécution conserve
la chronologie d'une tâche. Ces deux responsabilités ne doivent pas être confondues.

## Sous-agents et équipe de développement

La capacité `agent` crée un enfant lié au parent. La lignée normalisée interdit de
redéléguer exactement une tâche ancestrale et la profondeur suit la configuration. Les
enfants héritent du projet, des exigences nécessaires et des skills demandés.

`devteam.build_project` orchestre architecture, sécurité, code, tests, réparation et
documentation. Il demande `devteam.approve_quality_gate` avant de présenter le projet
comme livré. Ce pipeline est une fonctionnalité spécialisée, pas le comportement imposé
à toutes les tâches GPTMOSS.

## Assurance et livraison professionnelle

Après toutes les étapes :

1. le moteur évalue le contrat gelé à partir du workspace et des historiques réels ;
2. toute défaillance déclenche une réparation ciblée ou échoue explicitement ;
3. `DeliveryAssuranceCompleted` enregistre contrôles et preuves ;
4. une tâche documentaire professionnelle génère un DOCX, le rapport d'assurance JSON,
   un manifeste SHA-256 et un ZIP ;
5. l'état devient `completed` seulement après convergence ;
6. la GUI affiche le bouton de téléchargement uniquement si le paquet existe.

Les critères documentaires interdisent notamment les placeholders, doublons et volumes
insuffisants, et exigent couverture des pièces et références locales selon le profil.

## Réglages à chaud

La GUI lit `/api/settings`, masque la clé et soumet le modèle complet. L'API exige une
confirmation pour les changements sensibles, écrit `config.json`, met à jour fournisseur,
politique, shell, limites, registres et capacités, puis écrit l'audit. Un changement de
workspace resynchronise artefacts et documents. Le test de connexion vérifie à la fois le
catalogue `/models` et une complétion minimale, donc une clé sans droit d'inférence est
détectée avant une vraie exécution.

## Suppression et conservation

- Une annulation conserve les traces mais arrête les processus connus.
- La suppression d'une exécution refuse un travail encore actif et propage l'événement.
- La suppression d'un artefact retire fichier, métadonnées et index.
- Seuls les skills appartenant au workspace sont modifiables ou supprimables.
- `clear-all` ne doit pas masquer une exécution active.
- Les paquets livrés restent associés au projet, séparés des uploads et du dépôt source.
