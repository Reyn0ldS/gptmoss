# Guide d'Installation - GPT-Moss Agentic Client

Ce document décrit en détail les étapes d'installation et de configuration de la plateforme d'agents GPT-Moss.

## Pré-requis

* **Python** : Version 3.10 ou supérieure.
* **Pip** : Gestionnaire de paquets standard de Python.
* **Accès réseau** : Connexion à l'API LLM configurée (par exemple, le serveur vLLM distant de Qwen).

---

## 1. Installation Automatique (Recommandée)

### Sous Windows
1. Double-cliquez sur `install.bat` ou lancez-le depuis votre terminal.
2. Le script va créer automatiquement un environnement virtuel (`venv`), mettre à jour `pip` et installer toutes les dépendances requises depuis `requirements.txt`.
3. Il copiera également les gabarits de configuration par défaut (`.env` et `workspace/config.json`).

### Sous Linux / macOS
1. Ouvrez un terminal dans le répertoire racine et exécutez le script d'installation :
   ```bash
   bash install.sh
   ```
2. Le script va vérifier votre installation Python, créer l'environnement virtuel, installer les paquets pip requis, initialiser les configurations par défaut et attribuer les droits d'exécution nécessaires au script de démarrage (`start.sh`).

---

## 2. Configuration

### Étape A : Variables d'Environnement (`.env`)
Modifiez le fichier `.env` généré à la racine pour ajuster les accès à votre LLM :
```env
OPENAI_API_KEY=votre-cle-api-qwen # Clé API ou jeton d'authentification
OPENAI_BASE_URL=https://gpu01.quartz.moss/general/v1 # URL de l'API compatible OpenAI
OPENAI_MODEL_NAME=Qwen/Qwen3.6-35B # Nom exact du modèle dans le registre vLLM

# Options de vérification SSL (Utile si votre serveur utilise des certificats locaux auto-signés)
SSL_VERIFY=False
SSL_CERT_PATH=

# Adresse et port d'écoute du serveur de console Web
MOSS_HOST=127.0.0.1
MOSS_PORT=8000
```

### Étape B : Fichier de Configuration Workspace (`workspace/config.json`)
Ce fichier permet d'ajuster les contraintes de sécurité et les projets de la plateforme :
```json
{
  "api_key": "mock-key",
  "base_url": "https://gpu01.quartz.moss/general/v1",
  "model_name": "Qwen/Qwen3.6-35B",
  "ssl_verify": false,
  "ssl_cert_path": "",
  "denied_capabilities": [], // Capacités interdites à l'agent
  "approval_required_capabilities": [ // Outils nécessitant une confirmation humaine (HIL)
    "shell",
    "devteam.approve_quality_gate"
  ],
  "workspace_path": "./workspace", // Racine du répertoire autorisé
  "restrict_to_workspace": true, // Interdire la modification de fichiers hors workspace
  "allow_subfolders": true, // Autoriser les sous-dossiers
  "projects": [ // Liste des projets disponibles dans l'interface
    {
      "id": "proj-default",
      "name": "Projet Par Défaut"
    }
  ]
}
```

---

## 3. Lancement

### Sous Windows
Exécutez `start.bat`.

### Sous Linux / macOS
Exécutez dans votre terminal :
```bash
./start.sh
```

Une fois le serveur démarré, ouvrez votre navigateur et accédez à :
```
http://127.0.0.1:8000/
```

---

## 4. Dépannage (Troubleshooting)

* **Erreur d'appel d'outil (400 Bad Request)** : Si votre serveur LLM (par exemple vLLM) ne supporte pas l'auto-tool-choice natif, la plateforme active automatiquement notre module de repli prompt-based. Les requêtes se résoudront normalement après une brève tentative.
* **Erreur SSL** : Si vous rencontrez des problèmes de négociation de certificat, assurez-vous que `SSL_VERIFY=False` est correctement configuré dans votre fichier `.env`.
* **Verrous de fichiers sous Windows** : En cas de collisions lors de l'enregistrement simultané de multiples sous-agents, la plateforme intègre un système de verrouillage robuste avec retours exponentiels.






# Manuel d'Utilisation - GPT-Moss Agentic Client

Bienvenue dans le manuel d'utilisation du client agentique GPT-Moss. Cette plateforme orchestre un ensemble d'agents spécialistes autonomes pour accomplir vos tâches logicielles et documentaires.

---

## 1. Interface Graphique (Console Web)

La console Web est accessible sur `http://127.0.0.1:8000/`. Elle propose une vue unifiée en temps réel :

* **Barre Latérale (Gestion de Tâches)** : Affiche la liste de vos tâches courantes. Les tâches sous-agents (spécialistes) sont imbriquées hiérarchiquement sous la tâche principale avec des lignes d'indendation claires, vous permettant de suivre l'arbre de délégation en direct.
* **Dialogue Inter-Agents (Discussion)** : Flux de dialogue chronologique propre montrant les réflexions et les résultats textuels de chaque spécialiste (ex: Architecte, Développeur, Revue Sécurité). Les messages système internes redondants y sont filtrés pour éviter toute pollution visuelle.
* **Liste d'étapes (Plan DAG)** : Affiche les étapes planifiées par le coordinateur. Chaque étape détaille ses dépendances (ex: `Dépend de : #1, #2`), sa description et son état actuel (Pending, Running, Completed, Failed).
* **Bannière d'Approbation Humaine (Human-in-the-Loop)** : Si une action sensible est déclenchée (ex: exécution d'un script shell ou passage d'une porte de qualité), le moteur suspend l'agent, affiche une bannière jaune d'alerte et attend votre accord ("Autoriser" ou "Refuser" avec commentaire optionnel).

---

## 2. Le Pipeline Multi-Agent & Rôles Spécialisés

Lorsque vous soumettez une tâche de développement logiciel, le coordinateur planifie un cycle de développement (SDLC) complet composé de rôles spécialisés :

1. **Architecte** : Rédige le document de conception technique `specs.md` définissant les protocoles, schémas de fichiers et structures globales.
2. **Analyste Sécurité** : Analyse les spécifications de conception et génère un rapport `security_review.md` contenant des propositions de correctifs de vulnérabilités.
3. **Développeur** : Implémente le code source réel du projet dans les fichiers respectifs sans ajouter de simples placeholders.
4. **Testeur QA** : Conçoit et écrit la suite de tests unitaires (généralement sous pytest).
5. **Débugueur** : Exécute le code et les tests unitaires. En cas d'échec, il lit les journaux d'erreurs et corrige les fichiers sources de manière itérative.
6. **Rédacteur Technique** : Rédige le fichier de documentation d'utilisation et d'installation `README.md` final du projet.

---

## 3. Planification Concurrente (DAG Scheduler)

GPT-Moss utilise un moteur d'ordonnancement par graphe orienté acyclique (DAG).
* **Parallélisme** : Les étapes qui n'ont pas de dépendances directes entre elles sont exécutées en parallèle (par exemple, la rédaction d'un module de chiffrement et la conception de la maquette de l'interface utilisateur peuvent tourner en même temps).
* **Respect des verrous** : Une étape ne démarre que lorsque l'intégralité de ses dépendances a été complétée avec succès.
* **Sécurité antidécurrence** : Le graphe intègre une détection de dépendances cycliques pour éviter les blocages. Si une étape échoue, les autres tâches actives sont annulées de façon propre.

---

## 4. Restriction des Capacités pour les Sous-Agents

Pour garantir l'efficacité de la plateforme, les sous-agents (spécialistes) subissent une restriction de capacités par rapport au coordinateur :
* **Délégation Interdite** : Les spécialistes n'ont pas accès aux outils `agent` et `devteam` (ils ne peuvent pas créer d'autres sous-agents ou initier de nouveaux projets).
* **Focus Métier** : Ils emploient exclusivement les outils de système de fichiers (`filesystem`) et d'exécution shell (`shell`) pour accomplir directement leur tâche.

---

## 5. Mode Terminal (CLI)

Vous pouvez lancer l'exécution d'une tâche unitaire directement depuis votre terminal sans démarrer le serveur Web :
```bash
# Activation de l'environnement virtuel
source venv/bin/activate  # ou call venv\Scripts\activate.bat sous Windows

# Lancement de la tâche
python main.py --task "Crée un projet calculator en python avec tests unitaires pytest"
```
En mode CLI, les requêtes d'approbations humaines s'affichent directement dans le terminal sous forme de prompt interactif `Approve action? (y/n)`.
