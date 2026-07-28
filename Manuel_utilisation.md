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

Sous Windows, `start.bat --task "..."` sélectionne automatiquement le `venv`, le Python portable embarqué ou le Python système. Un runtime `python-*-embed-amd64` doit avoir été préparé avec `scripts/prepare_portable_python.py` avant son transfert vers une machine hors-ligne.

---

## 6. Centre de contrôle Web

Le bouton **Bibliothèque** ouvre le centre d'administration complet :

* **Documents et images** : aperçu local, rattachement à la prochaine tâche et suppression confirmée ;
* **Skills** : création, import `SKILL.md`, modification, validation de compatibilité, activation et suppression des skills du workspace ;
* **Mémoire** : création, recherche, modification, provenance, expiration, validation et suppression ;
* **Sous-agents** : création sous l'exécution sélectionnée, pause, reprise et annulation ;
* **Diagnostics** : modèle, vision, capacités/actions, états, métriques, traces et erreurs ;
* **Audit** : historique local expurgé des changements sensibles.

Dans **Paramètres**, la clé API peut être affichée volontairement pendant 15 secondes, uniquement depuis la machine locale. L'action est auditée mais le secret ne l'est jamais. **Tester la connexion** contacte réellement l'endpoint `/models` du fournisseur après avertissement. Avant toute sauvegarde, la GUI résume les champs modifiés ; les protections affaiblies nécessitent de saisir `CONFIRMER`.

Pour les procédures détaillées, limites de formats, routes API et règles de sécurité, consultez la section **Centre de contrôle GUI : mode d'emploi complet** du [README](README.md#centre-de-contrôle-gui--mode-demploi-complet).
