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
