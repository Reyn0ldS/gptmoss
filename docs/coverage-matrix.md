# Matrice de couverture fonctionnelle

La matrice relie chaque domaine à son code propriétaire, ses surfaces externes, sa
configuration et ses preuves. La liste exhaustive des routes, actions, événements,
modules et tests est dans `application-map.json` ; le présent document apporte le sens.

| Domaine | Code propriétaire | GUI/API | Configuration | Preuves principales |
|---|---|---|---|---|
| Bootstrap | `main.py`, scripts install/start | `/health`, `/readiness` | workspace, fournisseur, limites | `test_runtime_improvements`, `test_windows_portable_python` |
| Supervision serveur | `server_supervisor.py` | centre Serveur, `/api/runtime-control`, API :8765 | ports CLI/environnement | `test_server_supervisor`, `test_functional_coverage_contract` |
| Soumission/projets | kernel, API, filesystem | compositeur, `/executions`, `/projects` | `projects`, `workspace_path` | `test_api`, `test_end_to_end_workflow` |
| Planification | `planners/simple.py`, execution | plan et statuts | contexte, autonomie, délégation | `test_planning_modes`, `test_lifecycle_chronology` |
| Exécution/outils | `core/execution.py`, capacités | timeline, feed, WebSocket | budgets, skills, permissions | `test_execution`, `test_adaptive_autonomy` |
| Politique/shell | policy, shell/filesystem | approbation, pause/reprise | denied/approval, safe shell, autonomie | `test_policy`, `test_runtime_improvements` |
| Sous-agents | agent, devteam, kernel | bibliothèque et endpoints subagents | profondeur/délégation | `test_agent_and_memory`, `test_devteam` |
| Pièces jointes | artifacts, documents | upload, liste, recherche, aperçu | limites upload/texte | `test_documents`, `test_document_capability`, `test_skills_and_artifacts` |
| Mémoire | JSON store, context, memory cap | bibliothèque `/memory` | scope projet implicite | `test_memory_v2` |
| Skills/évolution | skills, evolution | bibliothèque, profils, évolution | skills par défaut, création/amélioration | `test_autonomous_evolution`, `test_skills_and_artifacts` |
| Fournisseur LLM | qwen, reprise execution | test de connexion, diagnostics | URL, clé, modèle, TLS, vision | `test_provider_integration`, `test_api` |
| Qualité documentaire | corpus, document_quality | résultats d'exécution | profil professionnel | `test_document_quality`, `test_corpus` |
| Assurance logiciel | delivery, artifact_validation | plan, métriques, feed | contrat produit par plan | `test_delivery_assurance`, `test_delivery_benchmarks` |
| Paquet professionnel | professional_delivery, delivery_package | bouton Télécharger | profil professionnel | `test_professional_delivery` |
| État/événements | state, event_bus, observability | WebSockets, diagnostics, audit | fichiers du workspace | `test_event_bus`, `test_lifecycle_chronology` |
| Offline | scripts préparation, manifests | scripts BAT/install/start | versions épinglées | `test_offline_source_package`, `test_windows_portable_python` |
| Documentation vivante | docs + validateur | dépôt | manifeste cartographique | `test_application_map`, `test_documentation_contract` |

## Contrats de configuration

| Groupe | Champs | Application effective |
|---|---|---|
| Fournisseur | `api_key`, `base_url`, `model_name`, `vision_mode`, `ssl_verify`, `ssl_cert_path` | QwenProvider ; test catalogue et chat ; clé non réaffichée par défaut. |
| Politique | `denied_capabilities`, `approval_required_capabilities`, `workspace_full_autonomy` | `SimplePolicyProvider` avant chaque outil ; refus prioritaire sur autonomie. |
| Orchestration | `continue_while_progress`, `adaptive_resource_management`, `max_step_iterations`, `max_step_retries` | Budgets de stagnation/reprise et compilation adaptative. |
| Délégation | `allow_nested_delegation`, `max_delegation_depth` | Schémas d'outils, lignée et contrôles du noyau. |
| Évolution | `autonomous_specialization`, création/amélioration, seuil/cap | Registres de profils et cycle de vie des skills. |
| Workspace | chemin, restriction, sous-dossiers, projets | Filesystem, shell, artifacts et résolveur par exécution. |
| Documents | `max_upload_bytes`, `max_attachment_text_chars`, `max_context_chars` | Dépôt, normalisation et compilation contextuelle. |
| Shell | `safe_shell_mode`, timeout, sortie maximale | Validation de commande, processus et rendu de résultat. |
| Skills | `strict_skill_capabilities`, `default_skills` | Sélection procédurale ; restriction des outils seulement en mode strict. |

Le validateur exige une correspondance exacte entre les clés du template et celles de la
carte. Les tests API vérifient le modèle, la persistance et l'application à chaud ; une
nouvelle clé ne doit pas être considérée couverte par la seule présence JSON.

## Contrats GUI/API

| Surface GUI | Services indispensables | Retour attendu dans la GUI |
|---|---|---|
| Compositeur | projets, artefacts, skills, création exécution | tâche créée, sélection conservée, erreurs explicites |
| Suivi | liste/détail, feed unifié, WebSocket | statut, plan, étapes, outils, approbations en temps réel |
| Contrôles d'exécution | pause, reprise, annulation, suppression | boutons activés selon l'état réel et retour serveur |
| Livraison | endpoint delivery | bouton visible uniquement si manifeste/ZIP disponibles |
| Bibliothèque | artefacts, recherche, mémoire, skills, diagnostics, audit | inventaire actualisé après chaque mutation |
| Réglages | lecture, écriture, révélation, test fournisseur | secret masqué, confirmation sensible, diagnostic précis |
| Serveur | découverte du superviseur et actions de contrôle | état `starting/running/stopped/error`, port et erreur actualisés |

La carte vérifie la présence des fonctions et chemins essentiels dans `gui.html`. Les
tests contractuels vérifient les comportements clés ; les parcours critiques restent à
exécuter en test d'intégration lorsqu'un navigateur ou un vrai fournisseur est requis.

## Formats documentaires

| Famille | Détection | Unité de provenance | Limites/risques couverts |
|---|---|---|---|
| Texte, Markdown, CSV, JSON, XML | signature/contenu et suffixe supporté | lignes/blocs | encodage, texte vide, contexte borné |
| HTML | parseur local | titres, paragraphes, listes, tables | aucune ressource externe exécutée |
| DOCX | signature ZIP + membres OOXML | paragraphes/tables | archive chiffrée, taille, ratio, traversée |
| PPTX | signature ZIP + membres OOXML | diapositives/blocs | mêmes frontières d'archive |
| PDF | signature `%PDF` et `pypdf` | page | pages vides signalées, pas d'OCR implicite |

## Niveaux de preuve

1. **Contrat statique** : manifeste, AST, inventaire des fichiers et correspondance GUI.
2. **Unitaire** : parseurs, politiques, mémoire, capacités et fonctions de qualité.
3. **Intégration** : API, persistance, exécution, reprise, superviseur et paquet offline.
4. **Parcours complet** : soumission jusqu'au livrable avec mock LLM contrôlé.
5. **Qualification environnementale** : runtime embarqué, reconstruction/verification
   offline, navigateur réel et fournisseur réel lorsque disponibles.

Une fonctionnalité n'est pas « pleinement qualifiée » si elle ne possède que le premier
niveau. La carte garantit surtout qu'aucune surface ne disparaît silencieusement ; les
tests attachés déterminent la profondeur de preuve.

## Procédure de changement

1. Identifier le domaine et le propriétaire dans cette matrice.
2. Mettre à jour interfaces, chronologie, configuration et frontières concernées.
3. Ajouter ou adapter les tests au niveau de risque approprié.
4. Mettre à jour `application-map.json` si une surface inventoriée change.
5. Exécuter `python scripts/validate_application_map.py` puis la suite complète.
6. Pour une dépendance ou un script de lancement, appliquer aussi la qualification offline.
