# GPTMOSS

GPTMOSS est une plateforme locale d'orchestration d'agents IA, avec interface Web, API FastAPI, modèles compatibles OpenAI et outils contrôlés.

## Capacités

- Exécutions planifiées, persistantes et reprenables avec validations humaines.
- Fichiers et shell soumis à une politique de capacités ; le shell demande une approbation par défaut.
- Mémoire hybride : session éphémère et mémoire persistante indexée, avec provenance, expiration et validation avant réutilisation.
- Compactage du contexte, télémétrie locale par exécution et métriques API.
- Skills déclaratifs (`SKILL.md`) pour spécialiser les agents et limiter leurs capacités.
- Artefacts texte, JSON, CSV et images, rattachables aux tâches.

## Installation

Prérequis : Python 3.10+ et une API compatible OpenAI (Qwen, vLLM ou équivalent).

```powershell
# Windows
.\install.bat

# Installation manuelle
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force workspace | Out-Null
Copy-Item config.json.template workspace/config.json
python main.py --workspace .\workspace
```

Sous Linux ou macOS : `bash install.sh`. L'interface est disponible sur <http://127.0.0.1:8000>.

Pour lancer une tâche directement :

```powershell
python main.py --workspace .\workspace --task "Analyse ce projet et propose des améliorations."
```

Les options `--host`, `--port` et `--workspace` permettent d'adapter le lancement.

## Configuration

Configurez `workspace/config.json`. Les variables `OPENAI_API_KEY`, `OPENAI_BASE_URL` et `OPENAI_MODEL_NAME` sont des valeurs de secours.

```json
{
  "api_key": "votre-cle",
  "base_url": "https://votre-serveur/v1",
  "model_name": "votre-modele",
  "ssl_verify": true,
  "workspace_path": "./workspace",
  "restrict_to_workspace": true,
  "allow_subfolders": true,
  "max_context_chars": 12000,
  "denied_capabilities": [],
  "approval_required_capabilities": ["shell", "devteam.approve_quality_gate"]
}
```

`max_context_chars` est borné entre 2 000 et 100 000 caractères. Conservez `ssl_verify: true` sauf besoin explicite et ne placez jamais une clé réelle dans Git.

## Mémoire, traces et skills

La mémoire de session est éphémère. La mémoire persistante (`workspace/memories.json`) garde provenance, date de création, durée de vie facultative et statut de validation ; les entrées non validées ne sont pas réutilisées automatiquement.

Le contexte est compacté avant les appels au modèle. Les traces sont écrites dans `workspace/telemetry.jsonl` avec masquage des champs sensibles connus. Les métriques sont exposées par `GET /executions/{execution_id}/metrics`.

Les skills sont recherchés dans `gptmoss/skills/` et `workspace/skills/`. Un skill est un dossier contenant ce fichier :

```markdown
---
name: analyse-securisee
description: Analyse du code avec des actions limitées.
allowed_capabilities: [filesystem]
---

Inspecte le projet et propose un correctif minimal.
```

Un skill sélectionné limite les outils exposés à ses capacités autorisées. Consultez [SKILLS.md](SKILLS.md) pour le format complet. La liste détectée est accessible avec `GET /skills`.

## Fichiers, API et sécurité

Envoyez un fichier avec `POST /artifacts`, puis placez les identifiants renvoyés dans `attachment_ids` d'un `POST /executions`. Les formats acceptés sont texte, Markdown, JSON, CSV, PNG, JPEG et WebP (10 Mio maximum).

Les fichiers texte sont ajoutés au contexte. Une image est transmise au modèle seulement si son nom indique la compatibilité vision (`vision`, `-vl` ou `omni`) ; sinon seules ses métadonnées sont fournies. PDF et documents Office ne sont pas encore pris en charge.

Routes utiles : `GET /executions`, `GET /executions/{id}`, `GET /executions/{id}/unified-feed`, `GET /skills`, `GET /api/settings` et `POST /api/settings`.

Les accès fichiers sont restreints au workspace par défaut ; les noms d'artefacts sont assainis et les signatures d'images vérifiées. Les garde-fous du shell ne remplacent pas une isolation complète du système : utilisez GPTMOSS dans un environnement de confiance.

## Tests et données locales

```powershell
python -m pytest -q
```

Le workspace contient `config.json`, `state_store.json`, `memories.json`, `telemetry.jsonl`, `uploads/` et `skills/`.