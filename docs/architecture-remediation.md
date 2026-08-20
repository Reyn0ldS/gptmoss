# Remédiation architecturale et garanties

Document **historique accepté** : les sept constats ci-dessous sont déjà livrés
dans `main`. La carte vivante est [architecture.md](architecture.md).

Ce document relie les sept constats de la cartographie aux composants, tests et critères
de validation maintenant versionnés. Les méthodes publiques historiques restent des
façades afin de préserver l'API Python, les fichiers persistés et le paquet offline.

## 1. Persistance atomique

`StateEngine` publie un index v3 avec `core.durable_io.write_text_atomic`. Les exécutions
et conversations sont des générations immuables adressées par SHA-256 : seules les
valeurs modifiées sont réécrites, l'index est committé après les sidecars, puis les
générations non référencées sont retirées. Une erreur avant le commit conserve le dernier
ensemble cohérent. L'identité et le digest sont revérifiés au chargement, les versions
v1/v2 sont migrées, et les versions futures ou corrompues sont mises en quarantaine. Preuves :
`tests/test_state_durability.py`.

## 2. Cycle de vie des événements

Les abonnements sont idempotents et révocables. La boucle de flush refuse les doubles
démarrages, retire son callback à l'arrêt, effectue une dernière sauvegarde puis annule sa
tâche. Preuves : `tests/test_event_bus.py`, `tests/test_state_durability.py` et
`tests/test_lifecycle_chronology.py`.

## 3. Machine d'état

`ExecutionStatus` définit les états acceptés et `ALLOWED_EXECUTION_TRANSITIONS` leurs
enchaînements. Chaque transition applicative conserve ancien état, nouvel état, motif,
acteur, corrélation et date. Les anciens JSON restent lisibles. Preuves : les tests de
durabilité, d'API, d'exécution et de délégation.

## 4. Façade d'exécution

`ExecutionEngine` délègue la reprise fournisseur à `ProviderRecoveryCoordinator`,
l'assurance à `DeliveryCoordinator` et les décisions humaines à
`ApprovalCoordinator`. La préparation d'une tâche, la coordination du DAG, l'exécution
d'une étape et le traitement des appels d'outils sont séparés. Les méthodes de façade
restent compatibles. `start_execution` est l'unique propriétaire des tâches actives ;
`cancel_active_execution` et `stop_active_executions` interrompent les appels LLM et
nettoient les étapes internes avant l'arrêt des transports.
Preuves : `tests/test_execution_services.py` et les suites d'exécution existantes.

## 5. Qwen et Shell

Le contexte Qwen et le parsing des tool calls sont des politiques pures. Le flux agrège
le texte, les appels d'outils et les statistiques de tokens dans un contrat commun ; le
repli textuel consomme le même format et les serveurs refusant `stream_options` sont
retentés sans l'extension optionnelle. Chaque reconfiguration ferme le client HTTP
remplacé et l'arrêt ferme le client actif. La
décision de sécurité shell, le registre des processus et le lanceur sont séparés de la
capacité ; stdout/stderr sont spoulés sur disque puis lus dans un budget borné.
Preuves :
`tests/test_provider_and_shell_services.py`, `tests/test_provider_integration.py` et
`tests/test_runtime_improvements.py`.

## 6. Surfaces auparavant dormantes

`Scheduler` ordonne les callbacks, annule les jobs, exécute les échéances et retente les
erreurs. Il est désormais l'unique service de délais pour les tâches planifiées, reprises
fournisseur et backoffs. Les partitions historiques restent chargeables mais sont
dépréciées au profit des projets et de la mémoire gouvernée. Preuves :
`tests/test_scheduler_and_legacy_state.py`.

## 7. Graphe GUI/API/scripts

Le graphe contient les fonctions et contrôles JavaScript, routes appelées, WebSockets et
invocations entre scripts. Un appel GUI littéral sans route backend alimente
`diagnostics.unresolved_gui_api_calls`. Ce contrôle a détecté et corrigé le GET erroné de
validation d'un skill, dont la route exige POST. Preuves : `tests/test_symbol_map.py` et
`tests/test_application_map.py`.

## 8. Généralisation et ressources

`ProjectDomainRegistry` fournit des domaines génériques et accepte des marqueurs par
projet ; les packs spécialisés déclarent `auto-select: false`. `RuntimeSettings` est le
contrat unique des limites et paramètres. Uploads, texte joint, transitions, contexte et
sorties shell ont des valeurs sûres. Preuves : tests d'autonomie, d'API, d'état et shell.

## 9. Qualification et publication

`document_quality_cases.json` mesure rappels et faux positifs des livrables. Le manifeste
source empêche la disparition silencieuse de code, GUI, skills, scripts ou docs. La CI
Linux/Windows valide lint, couverture, benchmarks, cartes, archive Git propre,
installation offline, démarrage et audit Edge.

## Qualification

Chaque domaine possède une suite ciblée. La qualification complète ajoute :

```powershell
python scripts/generate_symbol_map.py --check
python scripts/validate_application_map.py
python scripts/run_delivery_benchmarks.py
python scripts/run_quality_benchmarks.py
python scripts/verify_source_release.py
python -m pytest -q --cov=gptmoss --cov-fail-under=75
prepare-offline-source.bat --verify-only
```

Cette remédiation n'ajoute aucune dépendance externe, mais ses modules doivent être
présents dans l'archive source et passer les contrats offline.
