# GPTMOSS — manuel de référence

GPTMOSS est une plateforme locale d'orchestration d'agents IA. Elle combine un modèle compatible OpenAI, une API FastAPI, une interface Web et des capacités contrôlées pour planifier, exécuter et suivre des tâches.

Ce document est le mode d'emploi complet. Les exemples utilisent PowerShell et l'adresse locale `http://127.0.0.1:8000`.

La conception de la traçabilité, des propriétaires de fichiers, de l'audit
indépendant, des reprises LLM et du benchmark multi-prompts est détaillée dans
[DELIVERY_ASSURANCE.md](DELIVERY_ASSURANCE.md).

## Sommaire

- [Démarrage rapide](#démarrage-rapide)
- [Configuration complète](#configuration-complète)
- [Utiliser l'interface Web](#utiliser-linterface-web)
- [Utiliser la ligne de commande](#utiliser-la-ligne-de-commande)
- [Utiliser l'API](#utiliser-lapi)
- [Exécutions, plan et validations](#exécutions-plan-et-validations)
- [Capacités des agents](#capacités-des-agents)
- [Skills](#skills)
- [Fichiers, images et artefacts](#fichiers-images-et-artefacts)
- [Mémoire, contexte et traces](#mémoire-contexte-et-traces)
- [Sécurité](#sécurité)
- [Dépannage](#dépannage)
- [Tests du projet GPTMOSS](#tests-du-projet-gptmoss)
- [Garantie de livraison](DELIVERY_ASSURANCE.md)
- [Centre de contrôle GUI : mode d'emploi complet](#centre-de-contrôle-gui--mode-demploi-complet)

## Démarrage rapide

### Prérequis

- Python 3.10 ou plus récent ;
- une API de chat compatible OpenAI (Qwen, vLLM ou fournisseur équivalent) ;
- accès réseau à cette API ;
- Git n'est requis que pour travailler sur le code source.

Le mode hors-ligne signifie que GPTMOSS ne télécharge rien depuis Internet pendant
l'installation ou l'exécution. Un serveur de modèle compatible OpenAI doit néanmoins
être démarré sur la machine (`127.0.0.1`) ou accessible sur le réseau local. Le voyant
de l'interface indique la connexion au serveur GPTMOSS ; utilisez **Tester la
connexion** dans les paramètres pour vérifier séparément le modèle.

### Installation Windows

Depuis la racine du projet :

```powershell
.\install.bat
.\start.bat
```

`install.bat` crée un `venv` avec un Python complet ou configure directement le runtime portable, vérifie les dépendances, puis initialise `.env` et `workspace/config.json` s'ils n'existent pas. `start.bat` démarre ensuite le serveur sur le port 8000.

Les scripts Windows détectent automatiquement, dans cet ordre : un `venv` existant, un Python portable `python-*-embed-amd64` placé à la racine, puis le Python installé sur le système. La distribution Python *embeddable* ne contient ni `venv` ni `pip` ; GPTMOSS l'utilise donc directement comme runtime privé au lieu d'essayer de créer un environnement virtuel.

### Paquet Windows autonome hors-ligne

Le dépôt contient déjà Python 3.13 Win64 et toutes les dépendances dans `python-3.13.14-embed-amd64`. Téléchargez ou clonez le dépôt sur une machine connectée, puis transférez le dossier complet vers la machine isolée. Sur celle-ci, aucune préparation ni installation Python supplémentaire n'est nécessaire :

```powershell
.\install.bat
.\start.bat
```

`install.bat` configure le runtime embarqué, vérifie localement les imports et initialise les fichiers de configuration. Il ne télécharge rien en mode portable. `start.bat` sélectionne ensuite automatiquement ce runtime.

### Régénérer le paquet autonome sur une machine connectée

Après une modification de `requirements-runtime.txt` ou pour actualiser Python, exécutez sur Windows 64 bits avec une installation Python complète disposant de `pip` :

```powershell
.\prepare-offline-source.bat
```

Le constructeur télécharge CPython embeddable depuis Python.org, vérifie son SHA-256, résout uniquement les wheels d'exécution compatibles CPython 3.13/Windows amd64, les installe dans `Lib\site-packages`, teste le runtime et écrit `offline-runtime-manifest.json`. Le runtime préparé et le manifeste sont versionnés dans Git afin que les utilisateurs hors-ligne n'aient pas à répéter cette opération. `pytest` et `pytest-asyncio` font partie du runtime opérationnel : les agents QA peuvent ainsi tester les projets sans télécharger de paquet.

Ne copiez pas un `venv` entre deux machines : les environnements virtuels ne sont pas portables.

Avec une installation Python complète, une autre solution hors-ligne consiste à créer un dossier `wheelhouse` sur la machine connectée :

```powershell
python -m pip download --only-binary=:all: -d wheelhouse -r requirements.txt
```

Après transfert, `install.bat` détecte ce dossier et impose `--no-index`, ce qui interdit tout accès involontaire à Internet.

### Installation Linux/macOS

```bash
bash install.sh
./start.sh
```

### Installation manuelle

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force workspace | Out-Null
Copy-Item config.json.template workspace/config.json
python main.py --workspace .\workspace
```

Ouvrez ensuite <http://127.0.0.1:8000>. Pour arrêter le serveur au premier plan, utilisez `Ctrl+C`.

## Configuration complète

La configuration active est `workspace/config.json`. Au démarrage, GPTMOSS la normalise et la réécrit ; utilisez donc du JSON strict, sans commentaires.

```json
{
  "api_key": "votre-cle-api",
  "base_url": "https://votre-serveur/v1",
  "model_name": "votre-modele",
  "ssl_verify": true,
  "ssl_cert_path": "",
  "denied_capabilities": [],
  "approval_required_capabilities": [
    "shell",
    "devteam.approve_quality_gate"
  ],
  "workspace_full_autonomy": false,
  "continue_while_progress": true,
  "workspace_path": "./workspace",
  "restrict_to_workspace": true,
  "allow_subfolders": true,
  "max_context_chars": 12000,
  "max_step_iterations": 30,
  "max_step_retries": 2,
  "safe_shell_mode": true,
  "shell_timeout_seconds": 60,
  "shell_max_output_chars": 12000,
  "default_skills": [],
  "projects": [
    { "id": "proj-default", "name": "Projet par défaut" }
  ]
}
```

| Champ | Rôle | Valeur conseillée |
|---|---|---|
| `api_key` | Jeton transmis à l'API LLM. | Le définir dans une configuration locale, jamais dans Git. |
| `base_url` | Racine compatible OpenAI, habituellement terminée par `/v1`. | L'URL de votre fournisseur. |
| `model_name` | Nom exact du modèle de chat. | Celui exposé par votre fournisseur. |
| `ssl_verify` | Vérification TLS. | `true`. |
| `ssl_cert_path` | Chemin d'un certificat d'autorité personnalisé. | Vide, sauf PKI interne. |
| `denied_capabilities` | Capacités ou actions interdites. | Par ex. `['shell']` ou `['filesystem.delete']`. |
| `approval_required_capabilities` | Capacités ou actions nécessitant une décision humaine. | Conserver `shell` et le quality gate. |
| `workspace_full_autonomy` | Préautorise toutes les actions shell présentes et futures, uniquement dans les projets du workspace. Les refus explicites et le shell sûr restent prioritaires. | `false`, ou `true` sur un workspace isolé et de confiance. |
| `continue_while_progress` | Supprime la limite globale de tours d'une étape tant qu'un progrès durable est détecté. | `true` pour les tâches longues. |
| `workspace_path` | Racine de travail des agents. | Un dossier dédié. |
| `restrict_to_workspace` | Empêche les accès de fichiers hors de la racine. | `true`. |
| `allow_subfolders` | Autorise les opérations dans les sous-dossiers. | `true`. |
| `max_context_chars` | Budget du contexte conversationnel. Borné entre 2 000 et 100 000. | `12000`. |
| `max_step_iterations` | Avec le mode progrès actif : nombre de tours consécutifs sans progrès avant échec/reprise. Sinon : limite totale de tours. | `30` (1 à 100). |
| `max_step_retries` | Nombre de nouveaux spécialistes autonomes après l'échec d'une étape. | `2` (0 à 5). |
| `projects` | Projets proposés dans l'interface. | Voir ci-dessous. |
| `safe_shell_mode` | Active le blocage des commandes destructrices connues. | `true`. |
| `shell_timeout_seconds` | Délai maximal d'une commande shell. | `60` (1 à 600). |
| `shell_max_output_chars` | Taille maximale de sortie shell conservée. | `12000` (1 000 à 100 000). |
| `default_skills` | Skills appliqués par défaut. | `[]` pour la sélection automatique. |

Les variables d'environnement `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL_NAME` et `DASHSCOPE_API_KEY` servent de valeurs de secours quand le champ équivalent n'est pas renseigné. `main.py` charge `.env` au démarrage.

### Projets et dossiers de travail

Chaque exécution reçoit un `project_id`. Sans chemin personnalisé, ses opérations se font dans :

```text
<workspace>/projects/<project_id>/
```

Vous pouvez ajouter un projet à la configuration :

```json
{
  "id": "site-demo",
  "name": "Site de démonstration",
  "path": "D:/Projets/site-demo"
}
```

Un `path` personnalisé sort potentiellement du workspace : n'utilisez cette option qu'avec un répertoire explicitement choisi et de confiance. L'identifiant doit être simple (pas de `..`).

### Modifier les réglages à chaud

L'interface Web contient les réglages, organisés en panneaux Modèle, Sécurité shell, Skills et Projets. Elle permet aussi d'adapter les limites shell et les skills par défaut sans modifier les fichiers. Via API, lisez d'abord les valeurs actuelles :

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/settings
```

`POST /api/settings` attend tous les champs de configuration. La clé peut être laissée vide pour conserver la clé déjà chargée, mais évitez de journaliser sa valeur. Les changements de fournisseur, de politique et de répertoire sont appliqués sans redémarrage ; redémarrez toutefois le service si vous voulez une configuration simple et reproductible.

### Configurer GPTMOSS depuis la GUI

1. Ouvrez <http://127.0.0.1:8000> puis cliquez sur **Paramètres** dans le bas de la barre latérale.
2. Utilisez les boutons **Modèle**, **Sécurité shell**, **Skills** et **Projets** pour atteindre le panneau concerné.
3. Modifiez les valeurs, puis cliquez sur **Enregistrer**. Les valeurs sont appliquées au runtime et enregistrées dans `workspace/config.json`.
4. Si vous changez le dossier de travail, vérifiez les projets et les droits avant de soumettre une nouvelle tâche. Un redémarrage est conseillé après ce changement pour repartir d'un état simple.

#### Panneau Modèle

| Réglage GUI | Effet | Recommandation |
|---|---|---|
| Adresse API | URL compatible OpenAI du fournisseur. | Inclure `/v1` si le fournisseur l’exige. |
| Clé API | Secret du fournisseur. Le champ reste vide à la réouverture pour ne pas l’exposer. | Laisser vide si la clé actuelle doit être conservée. |
| Modèle par défaut | Modèle utilisé pour les nouvelles étapes. | Utiliser le nom exact fourni par l’API. |
| Vérifier le certificat SSL | Active la validation TLS ; le chemin de certificat apparaît si nécessaire. | Laisser activé. |
| Budget de contexte | Nombre de caractères de conversation donnés au modèle. | 12 000 est un bon départ ; augmenter si les tâches longues perdent du contexte. |
| Continuer tant que le travail progresse | Autorise une étape à dépasser tout nombre total de tours lorsque fichiers, livrables ou nouveaux tests réussis évoluent réellement. | Activé pour les projets longs. |
| Budget sans progrès | Nombre de tours consécutifs sans modification durable ni nouvelle preuve avant reprise ou échec. | 20 à 40 ; ce n'est pas une durée maximale. |
| Reprises autonomes | Nombre de nouveaux spécialistes chargés de reprendre les artefacts et preuves d'une tentative échouée. | 2 est un bon départ. |

#### Panneau Sécurité shell

| Réglage GUI | Effet | Recommandation |
|---|---|---|
| Mode shell sécurisé | Bloque des motifs de commandes destructrices connus. | Toujours activé. |
| Délai shell | Interrompt une commande trop longue (1 à 600 secondes). | 60 secondes. |
| Sortie shell maximale | Tronque les sorties trop volumineuses (1 000 à 100 000 caractères). | 12 000 caractères. |
| Validation humaine | Demande une confirmation avant les capacités cochées. | Conserver `shell` et le quality gate. |
| Capacités interdites | Refuse la capacité, même si le modèle la demande. | Bloquer `filesystem` ou `shell` pour un agent d’analyse seule. |
| Restriction du workspace | Empêche les accès hors du dossier de travail. | Toujours activée. |
| Sous-dossiers | Autorise la création et lecture dans les sous-dossiers. | Activé pour le développement ; désactiver pour limiter strictement l’agent. |
| Autonomie totale dans le workspace | Ne demande plus de confirmation humaine pour les commandes shell actuelles ou ajoutées plus tard. Ne désactive ni la restriction de chemin, ni les refus, ni le blocage destructif. | Seulement dans un workspace dédié et de confiance. |

Il n'existe pas de timeout global de projet. `shell_timeout_seconds` reste un garde-fou distinct pour une commande unique qui ne rend pas la main ; un projet peut enchaîner autant de commandes et d'étapes que nécessaire. En mode progrès, une modification de contenu, la création d'un livrable ou une nouvelle commande de vérification réussie remet le budget de stagnation à zéro. Répéter la même lecture, réécrire un contenu identique ou relancer la même commande ne le remet pas à zéro.

#### Panneau Skills

La liste affiche les skills détectés localement. Cocher un ou plusieurs skills les applique par défaut aux nouvelles exécutions et limite les capacités disponibles à celles qu’ils déclarent. Ne rien cocher conserve la sélection automatique selon le texte de la tâche.

Exemple : cochez `code-review` pour les revues, `test-and-debug` pour corriger une suite de tests, ou `documentation` pour générer une documentation. Utilisez **Enregistrer**, puis soumettez une nouvelle tâche : les exécutions déjà démarrées ne changent pas de skill.

#### Panneau Projets

Vous pouvez ajouter, renommer ou supprimer un projet, puis lui associer un dossier. Sans dossier personnalisé, les fichiers sont créés sous `<workspace>/projects/<id>/`. Un dossier personnalisé donne à l’agent accès à ce répertoire : sélectionnez uniquement un emplacement de confiance et conservez la restriction du workspace active lorsque possible.

## Utiliser l'interface Web

1. Lancez GPTMOSS et ouvrez <http://127.0.0.1:8000>.
2. Choisissez le projet puis décrivez la tâche avec un résultat attendu, les contraintes et les fichiers concernés.
3. Soumettez la tâche. La liste affiche son état et son plan ; le fil unifié rassemble les messages du coordinateur et des sous-agents.
4. Lorsqu'une action protégée est demandée, l'exécution passe à `paused`. Utilisez **Autoriser** ou **Refuser**, avec un motif si utile.
5. Consultez le résultat, les sous-tâches, les événements et les métriques. Une exécution terminée peut être supprimée de l'historique.

Conseil de formulation : précisez la portée, les critères d'acceptation et les contraintes. Exemple : « Dans le projet `site-demo`, analyse les tests existants, corrige uniquement les échecs reproductibles, exécute la suite puis résume les fichiers modifiés. »

## Utiliser la ligne de commande

Affichez les options :

```powershell
python main.py --help
```

Options disponibles :

```text
--host HOST           Adresse d'écoute (défaut : 127.0.0.1)
--port PORT           Port HTTP (défaut : 8000)
--workspace DOSSIER   Workspace local (défaut : ./workspace)
--task TEXTE          Exécute une tâche sans démarrer l'interface Web
```

Exemples :

```powershell
# Serveur local sur un autre port
python main.py --host 127.0.0.1 --port 8080 --workspace D:\GPTMOSS\workspace

# Tâche unique ; les demandes d'approbation sont posées dans le terminal
python main.py --workspace .\workspace --task "Analyse ce dépôt et propose un plan de correction."
```

N'exposez pas le serveur sur un réseau non fiable sans ajouter une authentification et des protections réseau : l'API n'implémente pas d'authentification applicative.

## Utiliser l'API

L'API est disponible lorsque le serveur tourne. Elle accepte et renvoie du JSON. FastAPI expose normalement sa documentation interactive sur `/docs`.

### Soumettre une exécution

```powershell
$body = @{
  task = "Relis le code du projet et rédige un rapport de risques."
  project_id = "proj-default"
  agent_config = @{
    system_prompt = "Tu es un relecteur méthodique. N'applique aucune modification sans l'expliquer."
    skills = @("code-review")
  }
} | ConvertTo-Json -Depth 5

$job = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/executions `
  -ContentType 'application/json' -Body $body
$job.execution_id
```

La réponse contient `execution_id` et l'état initial `running`. `agent_config.system_prompt` remplace l'instruction système par défaut. `agent_config.skills` est une liste de skills connus ; voir la section dédiée.

### Consulter une exécution

```powershell
$id = '<execution-id>'
Invoke-RestMethod "http://127.0.0.1:8000/executions/$id"
Invoke-RestMethod "http://127.0.0.1:8000/executions/$id/unified-feed"
Invoke-RestMethod "http://127.0.0.1:8000/executions/$id/metrics"
```

Routes principales :

| Méthode et route | Action |
|---|---|
| `GET /executions` | Liste des exécutions. |
| `GET /executions/{id}` | État, plan, variables et conversation. |
| `GET /executions/{id}/unified-feed` | Fil des messages, sous-agents compris. |
| `GET /executions/{id}/metrics` | Compteurs et durées de télémétrie. |
| `POST /executions` | Crée une exécution. |
| `GET /projects` | Liste les projets configurés. |
| `POST /projects` | Crée atomiquement un projet et son dossier. |
| `GET` / `POST /executions/{id}/subagents` | Liste ou crée les sous-agents d'une exécution parente. |
| `DELETE /executions/{id}` | Supprime une exécution et ses descendants de l'état persistant. |
| `POST /executions/clear-all` | Supprime tout l'historique d'exécution. |
| `GET /skills` | Liste les skills détectés. |
| `POST /skills` | Crée ou met à jour un skill local. |
| `POST /skills/import` | Importe le contenu d'un fichier `SKILL.md`. |
| `POST /skills/{name}/validate` | Vérifie le format, l'empreinte et la compatibilité d'un skill. |
| `DELETE /skills/{name}` | Supprime un skill du workspace ; les skills intégrés sont protégés. |
| `POST /artifacts` | Dépose un fichier. |
| `GET /artifacts/{id}/preview` | Renvoie un aperçu texte ou image local. |
| `GET` / `POST /memory` | Filtre les mémoires ou crée une entrée. |
| `PUT /memory/{id}` | Modifie valeur, provenance, validation et expiration. |
| `GET /api/diagnostics` | Capacités, compatibilité vision, métriques, traces et erreurs. |
| `GET /api/audit` | Journal local expurgé des changements de réglages. |
| `GET` / `POST /api/settings` | Lit ou modifie les réglages. |
| `POST /api/settings/test-connection` | Teste réellement l'endpoint OpenAI-compatible `/models`. |
| `POST /api/settings/reveal-secret` | Révèle temporairement la clé, uniquement en local et après confirmation. |

### Contrôler une exécution

```powershell
$id = '<execution-id>'
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/executions/$id/pause"
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/executions/$id/resume"
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/executions/$id/approve" -ContentType 'application/json' -Body '{"reason":"Commande vérifiée"}'
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/executions/$id/reject" -ContentType 'application/json' -Body '{"reason":"Action refusée"}'
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/executions/$id/cancel"
```

`/resume` ne convient pas à une pause d'approbation : dans ce cas, utilisez impérativement `/approve` ou `/reject`. `cancel` est possible seulement pour les états `pending`, `running` ou `paused`.

### Événements temps réel

- `ws://127.0.0.1:8000/ws/events` diffuse tous les événements ;
- `ws://127.0.0.1:8000/ws/executions/{id}` diffuse ceux d'une exécution.

Le client doit garder la connexion ouverte et peut envoyer un message périodique pour satisfaire la boucle de réception.

## Exécutions, plan et validations

Une tâche est d'abord classée selon sa taille, ses domaines et ses résultats attendus. Le plan adaptatif fournit un rôle canonique (`architect`, `security`, `developer`, `qa`, `debugger`, `writer` ou `coordinator`) et un profil métier distinct (`specialist`, `expertise`, artefacts, critères d'acceptation et commandes de vérification). Une demande mêlant vision, ML, géométrie 3D, vêtements, interface et confidentialité crée donc plusieurs ingénieurs spécialisés plutôt qu'un seul développeur générique. Le moteur rejette un plan complexe sous-dimensionné, les poids de modèles prétendument générés et les workflows vêtement qui omettent le corps complet.

Le moteur valide les identifiants, les références et l'absence de cycle avant de démarrer. Les étapes indépendantes s'exécutent en parallèle. Un spécialiste ne peut annoncer sa réussite qu'après création des artefacts non vides, exécution exacte des vérifications déclarées et remise d'un résultat JSON structuré. Les agents QA importent le code réel : les dépendances locales factices, mocks de remplacement et géométries aléatoires sont refusés.

Chaque étape spécialiste possède un seul sous-agent persistant. Son identifiant et
son résultat sont enregistrés avant et après l'exécution, ce qui empêche une reprise
ou un double clic de refaire le même travail. Les livraisons des dépendances sont
transmises explicitement à l'étape suivante. Le coordinateur final reçoit toutes les
livraisons dans l'ordre du plan, les synthétise sans relancer de sous-agent et expose
l'ensemble dans le champ `results` de l'exécution.

Après un échec, un nouveau spécialiste peut reprendre le même workspace sans refaire les dépendances déjà validées. Il reçoit les dernières erreurs de commandes et les contrats Python extraits des sources. Si une boucle n'arrive pas à créer un fichier texte requis, un contexte de secours court peut générer cet artefact, qui doit ensuite être relu et vérifié normalement.

Le pipeline logiciel ne bloque plus l'agent de réparation derrière une suite déjà verte : l'auteur QA doit d'abord produire des tests réellement importables et collectables, puis un agent de réparation exécute et corrige la suite unité/intégration. L'acceptation E2E ajoute ensuite ses scénarios, suivie d'une réparation finale des seules régressions restantes. Une indisponibilité temporaire du fournisseur LLM est retentée avec attente progressive sans effacer les fichiers du projet ; les erreurs permanentes d'authentification restent immédiates.

États possibles :

| État | Signification |
|---|---|
| `pending` | Créée, pas encore exécutée. |
| `running` | En planification ou exécution. |
| `paused` | Pause manuelle ou attente d'approbation. |
| `completed` | Toutes les étapes sont terminées. |
| `failed` | Une étape a échoué. |
| `cancelled` | Annulée par l'utilisateur ou le parent. |

Une règle de politique peut viser une capacité entière (`shell`) ou une action précise (`filesystem.delete`). Une action refusée n'est pas exécutée ; une action nécessitant une approbation suspend l'exécution et enregistre l'action et ses arguments dans l'état.

## Capacités des agents

| Capacité | Actions | Utilisation et limites |
|---|---|---|
| `filesystem` | `read`, `write`, `list_dir`, `delete` | Les chemins sont résolus par rapport au projet de l'exécution. `write` écrase un fichier existant ; `delete` ne supprime un dossier que s'il est vide. |
| `shell` | `execute` | Lance une commande dans le dossier du projet. Délai par défaut : 60 s ; sortie limitée à 12 000 caractères. La commande `python` utilise l'interpréteur courant. |
| `agent` | `spawn`, `status`, `execute_subtask` | Crée ou suit un sous-agent. Non disponible aux sous-agents. |
| `devteam` | `build_project`, `approve_quality_gate` | Pipeline de développement : architecture, revue sécurité, code, vérification, tests, débogage et documentation. Non disponible aux sous-agents. |

Le shell bloque en mode sûr plusieurs motifs destructifs évidents (`rm -rf /`, `format`, `diskpart`, `shutdown`, `reg delete`, etc.). Ce filtrage est intentionnellement limité : une politique stricte et un environnement isolé restent nécessaires.

Pour demander un projet complet, formulez par exemple : « Crée le projet `inventaire` : application Python avec API, tests pytest et README. Utilise le workflow équipe de développement. » L'agent peut choisir `devteam.build_project` si la politique et les skills exposent cette capacité.

## Skills

Les skills sont des instructions locales de confiance. GPTMOSS les découvre dans :

```text
gptmoss/skills/<nom>/SKILL.md
<workspace>/skills/<nom>/SKILL.md
```

Format :

```markdown
---
name: mon-skill
description: Objectif en une phrase.
allowed_capabilities: [filesystem, shell]
---

Instructions détaillées données à l'agent lorsqu'il sélectionne ce skill.
```

`name` doit contenir uniquement lettres minuscules, chiffres, `_` ou `-`. Les capabilities autorisées sont l'union de celles des skills sélectionnés : choisissez-les avec parcimonie. En plus des skills généraux (`secure-python`, `test-and-debug`, `project-architecture`, `documentation`, `code-review`), le paquet fournit des skills spécialisés : `requirements-feasibility`, `computer-vision-ml`, `geometry-3d`, `digital-garments`, `backend-api`, `frontend-3d`, `integration-delivery` et `biometric-privacy`.

Deux modes de sélection existent :

1. automatique : GPTMOSS classe les skills selon les mots de la tâche ;
2. explicite : envoyez `agent_config.skills`, comme dans l'exemple API précédent.

Après ajout ou modification d'un fichier `SKILL.md`, redémarrez le serveur afin de redécouvrir les skills. Pour vérifier le résultat :

```powershell
Invoke-RestMethod http://127.0.0.1:8000/skills
```

Consultez aussi [SKILLS.md](SKILLS.md) pour les règles de compatibilité de skills provenant d'autres écosystèmes.

## Fichiers, images et artefacts

Les artefacts sont stockés sous `<workspace>/uploads/`. Un fichier reçoit un identifiant UUID, un fichier de métadonnées et une empreinte SHA-256.

Types acceptés : `text/plain`, `text/markdown`, `application/json`, `text/csv`, `image/png`, `image/jpeg`, `image/webp`. Taille maximale : 10 Mio. Les noms sont assainis ; PNG, JPEG et WebP sont contrôlés par signature. PDF et DOCX ne sont pas pris en charge actuellement.

### Déposer un fichier puis le joindre à une tâche

```powershell
$bytes = [System.IO.File]::ReadAllBytes('C:\Temp\notes.md')
$upload = @{
  filename = 'notes.md'
  content_type = 'text/markdown'
  content_base64 = [Convert]::ToBase64String($bytes)
} | ConvertTo-Json

$artifact = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/artifacts `
  -ContentType 'application/json' -Body $upload

$task = @{
  task = 'Résume la note jointe et liste les décisions.'
  attachment_ids = @($artifact.id)
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/executions `
  -ContentType 'application/json' -Body $task
```

Le texte est ajouté au contexte, avec une limite de 50 000 caractères par artefact. Une image est envoyée au modèle seulement si son nom contient `vision`, `-vl` ou `omni`; sinon l'agent reçoit une notice et les métadonnées. Cette détection est une heuristique : vérifiez la compatibilité réelle de votre modèle.

## Mémoire, contexte et traces

| Élément | Fichier | Comportement |
|---|---|---|
| État et conversations | `state_store.json` | Exécutions, plans et messages persistants. |
| Mémoire persistante | `memories.json` | Entrées indexées avec provenance, date, durée de vie et validation. |
| Mémoire de session | mémoire du processus | Non persistée ; propre à la session. |
| Télémétrie | `telemetry.jsonl` | Événements horodatés et données assainies. |
| Artefacts | `uploads/` | Fichiers déposés et métadonnées. |

Le contexte conversationnel est compacté au-delà de `max_context_chars`; les sorties d'outils individuelles sont limitées à 3 000 caractères dans le contexte. Les recherches de mémoire automatique ne réutilisent que les entrées validées et non expirées. Les traces masquent les champs sensibles connus (`api_key`, `authorization`, `token`, `password`, `secret`).

## Sécurité

1. Gardez `restrict_to_workspace` et `ssl_verify` à `true`.
2. Laissez `shell` dans `approval_required_capabilities`.
3. Utilisez `denied_capabilities` pour bloquer les actions inutiles, par exemple `filesystem.delete`.
4. N'exposez pas le serveur au réseau public : il n'offre pas d'authentification HTTP intégrée.
5. Ne mettez ni clé API, ni conversations sensibles, ni artefacts confidentiels dans Git.
6. Utilisez un workspace dédié. Un agent autorisé à écrire ou à exécuter des commandes peut modifier son projet.
7. Sauvegardez les fichiers `state_store.json` et `memories.json` avant une opération de nettoyage.

## Dépannage

| Symptôme | Vérifications et correction |
|---|---|
| Erreur de connexion au LLM | Vérifier `base_url`, `api_key`, `model_name`, la connectivité et le certificat TLS. Les chemins sont sensibles à la casse : utiliser le `/v1` réellement exposé par le serveur. Pour une CA privée, renseigner de préférence `ssl_cert_path`. |
| Erreur TLS | Garder `ssl_verify: true`; pour une PKI interne, fournir `ssl_cert_path`. Désactiver la vérification seulement pour un environnement explicitement contrôlé. |
| L'outil n'est pas appelé | Vérifier la politique, les skills sélectionnés et la compatibilité tool-calling du modèle. GPTMOSS normalise les balises textuelles `<tool_call>` de Qwen et utilise un repli par prompt si l'appel natif échoue. |
| Exécution bloquée en pause | Lire l'état puis appeler `/approve` ou `/reject` si une approbation est en attente ; sinon `/resume`. |
| Accès fichier refusé | Le chemin sort du workspace, contient une traversée ou les sous-dossiers sont désactivés. Utiliser un chemin relatif au projet. |
| Commande shell expirée | Simplifier la commande ou la découper ; la limite par défaut est 60 secondes. |
| Image non analysée | Choisir un modèle vision dont le nom contient `vision`, `-vl` ou `omni`, ou fournir une transcription texte. |
| Skill absent | Vérifier le nom, le frontmatter, l'encodage UTF-8 et redémarrer le serveur. |
| `No module named venv` | Utiliser la dernière version complète du dépôt, qui contient le runtime préparé `python-3.13.14-embed-amd64`. Ne pas le remplacer par une archive embeddable nue. Le mode portable n'utilise pas `venv`. |
| Dépendances absentes sur la machine hors-ligne | Le runtime n'a pas été transféré complètement. Reprendre le paquet autonome depuis Git ou exécuter `prepare-offline-source.bat` sur la machine connectée avant de transférer tout le dossier. |
| État ou mémoire à remettre à zéro | Arrêter le serveur, sauvegarder puis supprimer explicitement les fichiers concernés du workspace. |

## Tests du projet GPTMOSS

Après installation des dépendances :

```powershell
python -m pytest -q
```

Les tests vérifient notamment l'API, le moteur d'exécution, les politiques, la mémoire, les skills et les artefacts.
## Centre de contrôle GUI : mode d'emploi complet

Le bouton **Bibliothèque** ouvre désormais le **Centre de contrôle GPTMOSS**. Il rassemble les fonctions qui ne doivent plus nécessiter de modifier directement un script ou un fichier Markdown.

### Documents et images

1. Pour un nouveau fichier, utilisez le sélecteur situé sous le texte de la tâche. Le fichier est téléversé au moment où vous cliquez sur **Démarrer l'exécution**.
2. Pour réutiliser un fichier déjà stocké, ouvrez **Bibliothèque**, section **Documents et images**, puis cochez-le. Le compteur confirme son rattachement à la prochaine tâche.
3. Cliquez sur **Aperçu** pour afficher localement le texte ou l'image. Le texte est limité à 50 000 caractères dans l'aperçu et le contexte.
4. Cliquez sur **Supprimer** puis confirmez. Le contenu et ses métadonnées sont retirés ensemble.
5. Après soumission réussie, la sélection de pièces jointes est remise à zéro pour éviter une réutilisation accidentelle.

Une image n'est réellement transmise au modèle que si le panneau **Diagnostics** indique `vision: true`. Dans le cas contraire, GPTMOSS transmet seulement une note et les métadonnées. Les formats et limites sont détaillés dans [Fichiers, images et artefacts](#fichiers-images-et-artefacts).

### Créer, importer, modifier, valider et activer un skill

- **Créer** : renseignez un nom conforme à `[a-z0-9_-]`, une description, des instructions et le minimum de capacités nécessaires, puis cliquez sur **Créer / mettre à jour**.
- **Importer** : choisissez un fichier `SKILL.md` de 262 Ko maximum. Son frontmatter est analysé côté serveur et les capacités inconnues sont refusées.
- **Modifier** : utilisez **Modifier** sur un skill du workspace, corrigez le formulaire, puis enregistrez. Les skills intégrés sont consultables et validables, mais protégés contre la modification et la suppression.
- **Valider** : **Valider** calcule l'empreinte et signale les outils non compatibles. Un skill vide ou utilisant un outil non mappé n'est pas considéré valide.
- **Activer/désactiver** : le bouton correspondant ajoute ou retire le skill des choix par défaut. Le panneau **Paramètres > Skills** montre la même sélection. Une exécution déjà lancée garde sa configuration initiale.
- **Supprimer** : la confirmation efface le dossier du skill local, y compris ses ressources associées, après vérification qu'il se trouve bien sous `<workspace>/skills/`.

### Créer, rechercher, modifier et valider une mémoire

La section **Mémoire persistante** permet de saisir le contenu, sa provenance, une expiration facultative en jours et son état de validation. Une mémoire non validée est conservée mais n'est pas réutilisée automatiquement par l'agent.

- utilisez le champ de filtre pour rechercher sans recharger la page ;
- **Modifier** recharge l'entrée dans le formulaire ;
- **Valider** autorise sa réutilisation future et enregistre l'auteur/date de validation ;
- **Supprimer** demande une confirmation et retire aussi l'entrée de l'index ;
- l'expiration est comprise entre 1 jour et 365 jours dans la GUI.

La mémoire de session reste éphémère et gérée automatiquement par le moteur. La GUI administre ici la mémoire persistante indexée.

### Créer et piloter des sous-agents

1. Sélectionnez d'abord une exécution dans la barre latérale : elle devient le parent.
2. Ouvrez **Bibliothèque > Sous-agents**, donnez un rôle, une tâche et éventuellement une instruction système.
3. Cliquez sur **Créer le sous-agent**. Il apparaît dans l'arbre des exécutions et dans ce panneau.
4. Utilisez **Pause**, **Reprendre** ou **Annuler**. Une pause provoquée par une validation humaine doit toujours être traitée avec **Autoriser/Refuser** dans le panneau d'exécution, pas avec **Reprendre**.

Les sous-agents créés explicitement héritent des politiques runtime et ne peuvent pas déléguer à leur tour les capacités `agent` ou `devteam` pendant leur exécution.

### Diagnostics, traces, erreurs, capacités et vision

La section **Diagnostics** affiche : le modèle et sa compatibilité vision annoncée, les capacités enregistrées et leurs actions, le nombre d'exécutions par état, les compteurs de télémétrie, les 100 dernières traces assainies et les événements d'échec. Actualisez le Centre de contrôle en le fermant puis en le rouvrant, ou après une action.

Les champs connus comme secrets sont expurgés par le collecteur de télémétrie. Les traces peuvent néanmoins contenir le texte de tâches : conservez le workspace local et hors de Git.

### Secrets, authentification et réglages sensibles

Dans **Paramètres > Modèle** :

1. le champ de clé reste vide à la réouverture ; vide signifie « conserver le secret chargé » ;
2. **Afficher 15 s** demande une confirmation, fonctionne uniquement depuis la machine locale, interdit la mise en cache HTTP et journalise l'action sans la valeur ;
3. **Tester la connexion** avertit avant d'envoyer la clé au `base_url`, appelle réellement `<base_url>/models`, contrôle le statut HTTP et indique si le modèle configuré est listé ;
4. **Enregistrer** présente d'abord la liste des champs modifiés sans afficher le secret ;
5. si TLS, la restriction workspace, le shell sûr ou la validation humaine du shell sont affaiblis, il faut saisir exactement `CONFIRMER`.

Ne testez jamais une clé contre une URL que vous ne contrôlez pas. GPTMOSS est lié à `127.0.0.1` par défaut ; n'utilisez pas `--host 0.0.0.0` sans authentification inverse, pare-feu et TLS.

### Journal d'audit local

Le journal `settings_audit.jsonl` est placé dans le workspace et ignoré par Git. Le panneau **Journal d'audit** montre jusqu'aux 200 dernières entrées : date, action, noms des champs modifiés, présence éventuelle d'un changement de secret et caractère sensible. Il ne contient ni ancienne valeur, ni nouvelle valeur, ni clé en clair. Les révélations volontaires de secret sont également enregistrées.
