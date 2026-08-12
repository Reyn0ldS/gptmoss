# Remédiation architecturale et garanties

Ce document relie les sept constats de la cartographie aux composants, tests et critères
de validation maintenant versionnés. Les méthodes publiques historiques restent des
façades afin de préserver l'API Python, les fichiers persistés et le paquet offline.

## 1. Persistance atomique

`StateEngine` sérialise un snapshot versionné et le publie avec
`core.durable_io.write_text_atomic`. Une erreur d'écriture ou de remplacement laisse le
dernier fichier valide intact. Le verrou est créé avec l'instance et protège les
sauvegardes concurrentes. Preuves : `tests/test_state_durability.py`.

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
`ApprovalCoordinator`. Les méthodes de façade restent compatibles.
Preuves : `tests/test_execution_services.py` et les suites d'exécution existantes.

## 5. Qwen et Shell

Le contexte Qwen et le parsing des tool calls sont des politiques pures. La décision de
sécurité shell, le registre des processus et le lanceur sont séparés de la capacité.
Preuves :
`tests/test_provider_and_shell_services.py`, `tests/test_provider_integration.py` et
`tests/test_runtime_improvements.py`.

## 6. Surfaces auparavant dormantes

`Scheduler` ordonne les callbacks, annule les jobs, exécute les échéances et retente les
erreurs. Les partitions historiques restent chargeables mais sont dépréciées au profit
des projets et de la mémoire gouvernée. Preuves :
`tests/test_scheduler_and_legacy_state.py`.

## 7. Graphe GUI/API/scripts

Le graphe contient les fonctions et contrôles JavaScript, routes appelées, WebSockets et
invocations entre scripts. Un appel GUI littéral sans route backend alimente
`diagnostics.unresolved_gui_api_calls`. Ce contrôle a détecté et corrigé le GET erroné de
validation d'un skill, dont la route exige POST. Preuves : `tests/test_symbol_map.py` et
`tests/test_application_map.py`.

## Qualification

Chaque domaine possède une suite ciblée. La qualification complète ajoute :

```powershell
python scripts/generate_symbol_map.py --check
python scripts/validate_application_map.py
python -m pytest -q
prepare-offline-source.bat --verify-only
```

Cette remédiation n'ajoute aucune dépendance externe, mais ses modules doivent être
présents dans l'archive source et passer les contrats offline.
