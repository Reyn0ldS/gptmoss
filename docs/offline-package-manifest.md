# Contrat du paquet offline GPTMOSS

Le paquet offline est le dépôt GPTMOSS complet accompagné d'un runtime CPython Windows
embarqué. `prepare-offline-source.bat` prépare ou vérifie ce runtime ; il ne télécharge
pas le code de l'application, qui doit provenir du clone ou de l'archive Git.

## Contenu obligatoire

| Élément | Rôle | Validation |
|---|---|---|
| `python-3.13.14-embed-amd64/` | Interpréteur et paquets d'exécution Win64 | répertoire, exécutable, imports, inventaire du manifeste |
| `requirements-runtime.txt` | dépendances directes exactes | chaque ligne épinglée `==` |
| `constraints-runtime.txt` | résolution transitive exacte | égalité avec les paquets du manifeste |
| `offline-runtime-manifest.json` | provenance Python, empreintes et inventaire | hashes normalisés LF et versions |
| `main.py`, `gptmoss/` | application complète | présents dans Git et importables |
| `start.bat`, `install.bat` | installation et lancement isolés | tests de sélection du runtime |
| `prepare-offline-source.bat` et scripts associés | reconstruction/diagnostic | fenêtre conservée, journal, code retour |
| `config.json.template`, `.env.template` | initialisation sans secret livré | copie seulement si absent |
| `tests/`, `pytest.ini` | qualification locale | pytest disponible dans le runtime |
| documentation et skills | fonctionnalités et mode opératoire | suivis dans Git |

La liste minimale machine de fichiers du dépôt est dans la section `offline` de
`application-map.json`. Elle protège les scripts et surfaces applicatives qui avaient
déjà disparu d'archives incomplètes.

## Chaîne de préparation connectée

```text
prepare-offline-source.bat
  -> choisit un Python utilisable
  -> prepare_offline_source_launcher.py (console + offline-preparation.log)
  -> prepare_offline_source.py
       -> télécharge CPython officiel si reconstruction
       -> vérifie le SHA-256 de la distribution
       -> résout des wheels CPython 3.13 / win_amd64 uniquement
       -> applique requirements + contraintes
       -> installe dans Lib/site-packages
       -> configure python313._pth
       -> vérifie imports et runtime
       -> remplace le runtime seulement après validation
       -> écrit offline-runtime-manifest.json
```

Une reconstruction ne doit pas détruire le dernier runtime utilisable avant que le
nouveau soit validé. Un ancien backup verrouillé peut être signalé et nettoyé plus tard ;
il ne doit pas invalider une nouvelle construction déjà réussie.

## Installation sur la machine isolée

1. Transférer le répertoire complet en conservant les fichiers cachés et binaires.
2. Exécuter `install.bat`. Le script choisit d'abord un `venv`, puis le runtime embarqué,
   puis un Python système. Avec le runtime embarqué, il n'appelle ni `pip` ni Internet.
3. Le configurateur autorise `Lib/site-packages` dans le fichier `._pth` et vérifie les
   imports indispensables.
4. Les fichiers `.env` et `workspace/config.json` ne sont créés que s'ils n'existent pas.
5. Exécuter `start.bat`, puis contrôler `/health`, `/readiness` et l'état du superviseur.

Le mode offline interdit les téléchargements pendant installation et exécution, mais un
serveur LLM compatible OpenAI doit rester disponible localement ou sur le réseau autorisé.

## Vérifications reproductibles

Sur une machine connectée après changement de dépendance :

```powershell
.\prepare-offline-source.bat
.\prepare-offline-source.bat --verify-only
```

La vérification seule ne télécharge rien. Elle contrôle notamment :

- version et architecture de Python ;
- présence des fichiers runtime ;
- empreintes des exigences et contraintes ;
- égalité des versions déclarées et installées ;
- imports FastAPI, HTTP, OpenAI, Pydantic, tests, WebSocket et PDF ;
- capacité de lancer les tests sans écrire de bytecode dans le dépôt.

La cartographie ajoute un contrôle sans exécuter le runtime :

```powershell
python .\scripts\validate_application_map.py
```

Il vérifie les fichiers obligatoires, les empreintes, les dépendances et la cohérence du
manifeste. Les tests spécialisés sont `test_offline_source_package.py` et
`test_windows_portable_python.py`.

## Changements qui imposent une reconstruction

- ajout, retrait ou changement de version dans `requirements-runtime.txt` ;
- changement des contraintes transitives ;
- nouvelle bibliothèque nécessaire à un parseur, une capacité ou un test livré ;
- changement de version CPython ou de cible Windows ;
- modification du configurateur `._pth` ou de la liste d'imports qualifiés.

Un changement de code GPTMOSS sans dépendance ne nécessite pas de reconstruire Python,
mais exige que l'archive Git contienne bien tous les fichiers versionnés.

## Diagnostic des échecs

| Symptôme | Cause probable | Preuve à relever |
|---|---|---|
| Fenêtre BAT qui se ferme | ancien lanceur ou sortie non conservée | `offline-preparation.log`, code retour |
| Aucun téléchargement | runtime déjà valide ou mode `--verify-only` | première phase du journal |
| Alias Microsoft Store | faux `python.exe` sans runtime/pip | chemin et test `sys.version_info` |
| Import absent offline | wheel non embarquée ou `._pth` incorrect | manifeste, `Lib/site-packages`, import direct |
| Fichiers applicatifs absents | archive/branche Git incomplète | `git ls-files`, carte des fichiers obligatoires |
| Runtime entier vu comme supprimé par Git | permissions/ACL locales illisibles | existence du dossier et droits, sans restaurer par checkout aveugle |
| Échec depuis un partage UNC | chemin courant ou écriture non compatible | journal avec chemin absolu, workspace et erreur OS |

## Critères d'acceptation

Le paquet est publiable seulement si le validateur cartographique, la suite pytest, la
préparation complète et `--verify-only` réussissent dans un environnement représentatif.
Si les ACL locales empêchent la lecture du runtime, la qualification embedded doit être
déclarée bloquée : le code et les métadonnées peuvent être vérifiés séparément, mais cela
ne remplace pas l'exécution réelle du binaire livré.
