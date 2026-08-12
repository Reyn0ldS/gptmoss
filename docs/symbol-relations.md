# Graphe relationnel et organisation des modifications

`docs/symbol-map.json` est le point central machine des relations internes de GPTMOSS.
Il est généré depuis le code Python par `scripts/generate_symbol_map.py`, puis interrogé
par `scripts/analyze_impact.py`. Il complète la cartographie fonctionnelle : la première
explique les responsabilités, le graphe indique précisément quels symboles et contrats
sont liés.

## Ce qui est cartographié

Chaque identifiant est stable et suit l'une de ces formes :

```text
module:gptmoss.core.execution
gptmoss.core.execution:ExecutionEngine
gptmoss.core.execution:ExecutionEngine.execute_task
data:configuration:max_step_retries
data:execution-variable:pending_approval
data:event:ExecutionCompleted
data:api-route:POST /executions
```

Le graphe inventorie :

- modules, classes, fonctions et méthodes, avec fichier, lignes et signature ;
- appartenance module → classe → méthode ;
- imports internes et réexportations ;
- héritage, types utilisés et composition par injection de dépendances ;
- appels internes résolus, y compris `self.service.method()` lorsque le type est inféré ;
- champs de modèles, attributs d'instance et constantes structurantes ;
- lectures et écritures des champs de configuration ;
- lectures et écritures de `ExecutionState.variables`, `results` et des champs de cycle ;
- variables d'environnement ;
- routes FastAPI, actions de capacité et événements publiés ;
- emplacements persistants identifiables par leurs littéraux ;
- fonctions de test qui appellent les symboles de production ;
- domaine architectural et fonctionnalité de couverture provenant de la carte principale.

Les variables locales temporaires ne sont pas inventoriées. Elles rendraient le graphe
bruyant sans améliorer l'organisation d'une évolution. Les données conservées sont celles
qui franchissent une frontière de méthode, de module, d'exécution, de configuration ou de
persistance.

## Relations disponibles

| Relation | Signification pour une modification |
|---|---|
| `contains` | propriétaire structurel du symbole |
| `imports` | module dépendant d'un module ou symbole interne |
| `inherits` | classe dont le contrat de base est consommé |
| `composes` | classe qui reçoit ou construit un autre composant |
| `uses_type`, `typed_as` | annotation ou attribut dépendant d'un type interne |
| `calls` | appel interne statiquement résolu |
| `reads`, `writes` | consommation ou production d'une donnée structurante |
| `owns`, `defines` | propriété d'un attribut, champ ou constante |
| `exposes` | route API ou action de capacité exposée |
| `publishes` | événement émis par le symbole |
| `accesses` | emplacement persistant explicitement référencé |

Chaque relation porte sa ligne source et un niveau de confiance. `exact` provient d'un
nom résolu directement ; `inferred` utilise une annotation ou une injection ; `literal`
signale une correspondance avec un emplacement persistant.

## Analyse d'impact avant modification

Pour une méthode précise :

```powershell
python .\scripts\analyze_impact.py ExecutionEngine.execute_task
```

Pour tous les symboles d'un fichier :

```powershell
python .\scripts\analyze_impact.py --file gptmoss/core/documents.py --depth 3
```

Pour intégrer le résultat dans un outil :

```powershell
python .\scripts\analyze_impact.py MemoryCapability.propose --json
```

Le rapport fournit :

- les appelants et consommateurs, classés par distance ;
- les dépendances directes ;
- les données structurantes touchées ;
- les routes, actions et événements concernés ;
- les fichiers et domaines impactés ;
- les tests directement ou transitivement concernés.

Une recherche ambiguë s'arrête et affiche les identifiants possibles. Cela évite de
préparer un changement à partir du mauvais `save`, `read` ou `execute`.

## Workflow central d'une évolution

1. Identifier le symbole ou fichier propriétaire avec `application-map.json`.
2. Exécuter `analyze_impact.py` et conserver routes, données, appelants et tests proposés.
3. Vérifier les frontières fonctionnelles dans `functional-map.md` et les responsabilités
   dans `architecture.md`.
4. Modifier ensemble producteur, consommateurs, modèles, persistance et GUI lorsque le
   rapport montre une frontière partagée.
5. Exécuter d'abord les tests listés par le rapport, puis la suite complète selon le risque.
6. Régénérer le graphe : `python scripts/generate_symbol_map.py`.
7. Vérifier les deux cartes : `python scripts/validate_application_map.py`.
8. Ne committer une évolution structurante que si `--check` ne détecte aucune dérive.

## Contrôle anti-dérive

```powershell
python .\scripts\generate_symbol_map.py --check
python .\scripts\validate_application_map.py
```

Le graphe contient une empreinte déterministe de tous les fichiers Python de production,
d'exploitation et de test. Une signature, classe, méthode, appel, route, donnée structurée
ou relation modifiée rend le fichier versionné obsolète jusqu'à sa régénération. Le
validateur principal appelle ce contrôle : la cartographie relationnelle ne peut donc pas
diverger silencieusement du code.

## Couverture frontend et exploitation

Le graphe couvre également les fonctions et contrôles de `gui.html`, les appels
`fetch`/`requestApi`, les WebSockets et les relations entre scripts BAT, PowerShell et
shell. Les relations `calls_api`, `opens_websocket`, `triggers` et `invokes_script`
relient ainsi l'interface et les chaînes d'installation/démarrage au backend.

Les URL frontend littérales sans route correspondante sont publiées dans
`diagnostics.unresolved_gui_api_calls`. Les URL entièrement dynamiques restent marquées
`calls_dynamic_api` et doivent être validées par leurs tests fonctionnels.

## Limites assumées

L'analyse est statique et n'exécute ni import ni application. Les appels construits par
réflexion, noms calculés, monkeypatching ou retour dynamique peuvent ne pas être résolus.
Ils ne sont pas transformés en fausses relations : le graphe privilégie une preuve exacte
ou une inférence typée. Les fonctions et callbacks imbriqués ne deviennent pas des
symboles autonomes ; leurs relations dynamiques doivent donc être contrôlées dans le
propriétaire et par les tests concernés.

L'analyse d'impact est un guide de préparation et de sélection de tests, pas une preuve
que tous les comportements dynamiques sont couverts. La suite de tests, les parcours GUI
et la qualification offline restent les preuves finales.
