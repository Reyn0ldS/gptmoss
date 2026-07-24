# GPTMOSS — manuel de référence

GPTMOSS est une plateforme locale d'orchestration d'agents IA. Elle combine un modèle compatible OpenAI, une API FastAPI, une interface Web et des capacités contrôlées pour planifier, exécuter et suivre des tâches.

Ce document est le mode d'emploi complet. Les exemples utilisent PowerShell et l'adresse locale `http://127.0.0.1:8000`.

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

## Démarrage rapide

### Prérequis

- Python 3.10 ou plus récent ;
- une API de chat compatible OpenAI (Qwen, vLLM ou fournisseur équivalent) ;
- accès réseau à cette API ;
- Git n'est requis que pour travailler sur le code source.

### Installation Windows

Depuis la racine du projet :

```powershell
.\install.bat
.\start.bat
```

`install.bat` crée `venv`, installe `requirements.txt`, puis initialise `.env` et `workspace/config.json` s'ils n'existent pas. `start.bat` démarre ensuite le serveur sur le port 8000.

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
  "workspace_path": "./workspace",
  "restrict_to_workspace": true,
  "allow_subfolders": true,
  "max_context_chars": 12000,
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
| `workspace_path` | Racine de travail des agents. | Un dossier dédié. |
| `restrict_to_workspace` | Empêche les accès de fichiers hors de la racine. | `true`. |
| `allow_subfolders` | Autorise les opérations dans les sous-dossiers. | `true`. |
| `max_context_chars` | Budget du contexte conversationnel. Borné entre 2 000 et 100 000. | `12000`. |
| `projects` | Projets proposés dans l'interface. | Voir ci-dessous. |

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

L'interface Web contient les réglages. Via API, lisez d'abord les valeurs actuelles :

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/settings
```

`POST /api/settings` attend tous les champs de configuration. La clé peut être laissée vide pour conserver la clé déjà chargée, mais évitez de journaliser sa valeur. Les changements de fournisseur, de politique et de répertoire sont appliqués sans redémarrage ; redémarrez toutefois le service si vous voulez une configuration simple et reproductible.

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
| `DELETE /executions/{id}` | Supprime une exécution et ses descendants de l'état persistant. |
| `POST /executions/clear-all` | Supprime tout l'historique d'exécution. |
| `GET /skills` | Liste les skills détectés. |
| `POST /artifacts` | Dépose un fichier. |
| `GET` / `POST /api/settings` | Lit ou modifie les réglages. |

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

Une tâche est planifiée en étapes reliées par dépendances. Les étapes sans dépendance commune peuvent s'exécuter en parallèle. Si une description d'étape correspond à un rôle (architecte, sécurité, développement, QA, débogage ou documentation), le coordinateur crée un sous-agent ; les sous-agents ne reçoivent pas les capacités `agent` ni `devteam`.

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

`name` doit contenir uniquement lettres minuscules, chiffres, `_` ou `-`. Les capabilities autorisées sont l'union de celles des skills sélectionnés : choisissez-les avec parcimonie. Les skills intégrés sont `secure-python`, `test-and-debug`, `project-architecture`, `documentation` et `code-review`.

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
| Erreur de connexion au LLM | Vérifier `base_url`, `api_key`, `model_name`, la connectivité et le certificat TLS. |
| Erreur TLS | Garder `ssl_verify: true`; pour une PKI interne, fournir `ssl_cert_path`. Désactiver la vérification seulement pour un environnement explicitement contrôlé. |
| L'outil n'est pas appelé | Vérifier la politique, les skills sélectionnés et la compatibilité tool-calling du modèle. GPTMOSS utilise un repli par prompt si l'appel d'outil natif échoue. |
| Exécution bloquée en pause | Lire l'état puis appeler `/approve` ou `/reject` si une approbation est en attente ; sinon `/resume`. |
| Accès fichier refusé | Le chemin sort du workspace, contient une traversée ou les sous-dossiers sont désactivés. Utiliser un chemin relatif au projet. |
| Commande shell expirée | Simplifier la commande ou la découper ; la limite par défaut est 60 secondes. |
| Image non analysée | Choisir un modèle vision dont le nom contient `vision`, `-vl` ou `omni`, ou fournir une transcription texte. |
| Skill absent | Vérifier le nom, le frontmatter, l'encodage UTF-8 et redémarrer le serveur. |
| État ou mémoire à remettre à zéro | Arrêter le serveur, sauvegarder puis supprimer explicitement les fichiers concernés du workspace. |

## Tests du projet GPTMOSS

Après installation des dépendances :

```powershell
python -m pytest -q
```

Les tests vérifient notamment l'API, le moteur d'exécution, les politiques, la mémoire, les skills et les artefacts.