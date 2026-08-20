# Matrice de couverture fonctionnelle

La matrice relie chaque domaine à son code propriétaire, ses surfaces externes, sa
configuration et ses preuves. La liste exhaustive des routes, actions, événements,
modules et tests est dans `application-map.json` ; le présent document apporte le sens.

| Domaine | Code propriétaire | GUI/API | Configuration | Preuves principales |
|---|---|---|---|---|
| Bootstrap | `main.py`, scripts install/start | `/health`, `/readiness` | workspace, fournisseur, limites | `test_runtime_improvements`, `test_windows_portable_python`, `test_browser_layout_audit` |
| Supervision serveur | `server_supervisor.py` | centre Serveur, `/api/runtime-control`, API :8765 | ports CLI/environnement | `test_server_supervisor`, `test_functional_coverage_contract` |
| Soumission/projets | kernel, API, filesystem, domains | compositeur, `/executions`, `/projects` | `projects`, domaines projet, `workspace_path` | `test_api`, `test_end_to_end_workflow`, `test_adaptive_autonomy` |
| Planification temporelle | scheduler, kernel, reprise fournisseur | `delay_seconds`, `run_at`, statuts | échéance persistée | `test_scheduler_and_legacy_state`, `test_api`, `test_lifecycle_chronology` |
| Planification fonctionnelle | `planners/simple.py`, `core/workload.py`, `core/plan_obligations.py`, `core/corpus_policy.py`, `core/delivery_feedback.py`, `core/evidence_graph.py`, `planners/complexity.py`, `planners/fallbacks.py`, domains, execution | mode auto/direct/équipe, politique corpus, obligations, arêtes typées, retours ciblés, graphe de preuves, partitions dynamiques | contexte, autonomie, délégation, concurrence | `test_generic_planning_and_context`, `test_plan_obligations`, `test_plan_graph`, `test_delivery_feedback`, `test_evidence_graph`, `test_planning_modes`, `test_lifecycle_chronology`, `test_runtime_ux` |
| Exécution/outils | `core/execution.py`, `core/execution_plan.py`, `core/execution_progress.py`, `core/execution_rescue.py`, capacités | timeline, feed, WebSocket | budgets, skills, permissions | `test_execution`, `test_adaptive_autonomy` |
| Politique/shell | policy, shell/filesystem | approbation, pause/reprise | denied/approval, safe shell, autonomie | `test_policy`, `test_runtime_improvements` |
| Sous-agents | agent, devteam, kernel | bibliothèque et endpoints subagents | profondeur/délégation | `test_agent_and_memory`, `test_devteam` |
| Pièces jointes et dossiers | artifacts, corpora, documents | upload, import récursif, reprise, progression, liste, recherche, aperçu | limites upload/texte/fichiers, chemins sûrs, SHA-256 | `test_documents`, `test_document_capability`, `test_skills_and_artifacts`, `test_api` |
| Mémoire | JSON store, context, memory cap | bibliothèque `/memory` | scope projet implicite | `test_memory_v2` |
| Skills/évolution | skills, evolution | bibliothèque, profils, évolution | skills par défaut, création/amélioration | `test_autonomous_evolution`, `test_skills_and_artifacts` |
| Fournisseur LLM | qwen, politique de fenêtre, reprise execution | test de connexion, diagnostics | URL, clé, modèle, TLS, vision, fenêtre/réserve de contexte | `test_generic_planning_and_context`, `test_provider_integration`, `test_api` |
| Qualité documentaire | corpus, document_quality, `long_document_engine`, `document_model`, `document_planning`, `core/diagrams/*` | résultats, panneau Document long, `GET /executions/{id}/document` | profil professionnel, checkpoints, diagrammes | `test_document_quality`, `test_quality_benchmarks`, `test_corpus`, `test_long_document_engine`, `test_diagrams_and_docx` |
| Assurance logiciel | `core/delivery.py`, `core/delivery_feedback.py`, `core/evidence_graph.py`, artifact_validation | plan, métriques, feed, `GET /executions/{id}/evidence-graph` | contrat produit par plan, arêtes typées, reprise ciblée | `test_delivery_assurance`, `test_delivery_benchmarks`, `test_delivery_feedback`, `test_evidence_graph`, `test_plan_graph` |
| Paquet professionnel | professional_delivery, delivery_package | bouton Télécharger | profil professionnel | `test_professional_delivery` |
| État/événements | state, event_bus, observability | WebSockets, flux LLM, diagnostics, audit | index + sidecars du workspace | `test_event_bus`, `test_lifecycle_chronology`, `test_state_durability`, `test_runtime_ux` |
| Offline/release | scripts préparation, manifests source/runtime | scripts BAT/install/start | versions épinglées | `test_offline_source_package`, `test_source_release`, `test_windows_portable_python`, CI archive propre |
| Documentation vivante | docs, graphe de symboles + validateurs | dépôt et CLI d'impact | manifestes cartographiques | `test_application_map`, `test_symbol_map`, `test_documentation_contract` |

## Contrats de configuration

| Groupe | Champs | Application effective |
|---|---|---|
| Fournisseur | `api_key`, `base_url`, `model_name`, `vision_mode`, `ssl_verify`, `ssl_cert_path`, `context_window_tokens`, `context_output_reserve_tokens` | QwenProvider ; préflight borné, apprentissage de limite, test catalogue et chat ; clé non réaffichée par défaut. |
| Politique | `denied_capabilities`, `approval_required_capabilities`, `workspace_full_autonomy` | `SimplePolicyProvider` avant chaque outil ; refus prioritaire sur autonomie. |
| Orchestration | `continue_while_progress`, `adaptive_resource_management`, `max_step_iterations`, `max_step_retries`, `max_parallel_plan_steps` | Budgets de stagnation/reprise, compilation adaptative et taille de vague simultanée (`0` automatique), sans plafond sur le total du DAG. |
| Délégation | `allow_nested_delegation`, `max_delegation_depth` | Schémas d'outils, lignée et contrôles du noyau. |
| Évolution | `autonomous_specialization`, création/amélioration, seuil/cap | Registres de profils et cycle de vie des skills. |
| Workspace | chemin, restriction, sous-dossiers, projets | Filesystem, shell, artifacts et résolveur par exécution. |
| Documents | `max_upload_bytes`, `max_attachment_text_chars`, `max_context_chars`, `document_engine_enabled`, `document_checkpoint_enabled`, `document_target_section_words`, `diagram_rendering`, `docx_embed_diagrams` | Dépôt, recherche, moteur de sections, checkpoints et diagrammes ; la fenêtre fournisseur reste le plafond final. |
| Persistance | `max_transitions_per_execution` | Historique de transitions par exécution (`≥ 100`, défaut 2000). |
| Shell | `safe_shell_mode`, timeout, sortie maximale (`≥ 1`) | Validation de commande, processus et rendu de résultat. |
| Skills | `strict_skill_capabilities`, `default_skills` | Sélection procédurale ; restriction des outils seulement en mode strict. |

Le validateur exige une correspondance exacte entre les clés du template et celles de la
carte. Les tests API vérifient le modèle, la persistance et l'application à chaud ; une
nouvelle clé ne doit pas être considérée couverte par la seule présence JSON.

## Contrats GUI/API

| Surface GUI | Services indispensables | Retour attendu dans la GUI |
|---|---|---|
| Compositeur | projets, artefacts, skills, création exécution | tâche créée, sélection conservée, erreurs explicites |
| Suivi | liste/détail, feed unifié, WebSocket, onglets Liste/Graphe, panneau Document long | statut, plan (`plan.edges`), étapes, outils, approbations, avancement des sections |
| Contrôles d'exécution | pause, reprise, annulation, suppression | boutons activés selon l'état réel et retour serveur |
| Livraison | endpoint delivery, `GET /executions/{id}/evidence-graph` | bouton visible uniquement si manifeste/ZIP disponibles. Le Graphe GUI dessine `plan.edges` ; le graphe de preuves est une vue API distincte. |
| Bibliothèque | artefacts, recherche, mémoire, skills, diagnostics, audit | inventaire actualisé après chaque mutation |
| Réglages | lecture, écriture, révélation, test fournisseur | secret masqué, confirmation sensible, diagnostic précis |
| Serveur | découverte du superviseur et actions de contrôle | état `starting/running/stopped/error`, port et erreur actualisés |

La carte vérifie la présence des fonctions et chemins essentiels dans `gui.html`. Les
tests contractuels vérifient les comportements clés ; les parcours critiques restent à
exécuter en test d'intégration lorsqu'un navigateur ou un vrai fournisseur est requis.

## Formats documentaires

| Famille | Détection | Unité de provenance | Limites/risques couverts |
|---|---|---|---|
| Texte, Markdown, CSV, JSON | signature/contenu et suffixe supporté | lignes/blocs | encodage, texte vide, contexte borné |
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
2. Exécuter `python scripts/analyze_impact.py <symbole>` ou `--file <chemin>`.
3. Mettre à jour interfaces, chronologie, configuration et frontières concernées.
4. Ajouter ou adapter les tests proposés au niveau de risque approprié.
5. Mettre à jour `application-map.json` si une surface inventoriée change, puis régénérer
   `symbol-map.json`.
6. Exécuter `python scripts/validate_application_map.py` puis la suite complète.
7. Pour une dépendance ou un script de lancement, appliquer aussi la qualification offline.
