# Workflow documentaire local de GPTMOSS

## 1. Objectif et contraintes

Ce chantier ajoute à GPTMOSS un workflow documentaire professionnel capable de lire, analyser, rechercher et synthétiser des corpus locaux volumineux, puis de produire des dossiers longs et cohérents.

Ordre de priorité des formats d'entrée :

1. DOCX ;
2. PPTX ;
3. TXT et Markdown ;
4. HTML local ;
5. PDF, dans une phase ultérieure et sans bloquer les quatre formats prioritaires.

Contraintes confirmées :

- toutes les sources sont des fichiers locaux ;
- le workflow ne suit et ne télécharge aucun lien Internet ;
- la qualité, la couverture et la cohérence du contenu priment sur la mise en forme finale ;
- le premier livrable est assemblé en Markdown lisible et portable ;
- les capacités s'adaptent au volume et au contexte disponibles, avec compactage et recherche plutôt qu'une troncature silencieuse ;
- les protections contre les archives malveillantes et les chemins non locaux restent actives ;
- chaque incrément est testé, corrigé, validé, documenté, puis poussé sur la branche dédiée.

Branche de développement : `agent/local-document-workflow`.

## 2. Périmètre fonctionnel

Le workflow cible les usages suivants :

- inventaire d'un corpus hétérogène ;
- extraction structurée des titres, paragraphes, listes, tableaux, diapositives et métadonnées utiles ;
- conservation d'une provenance vérifiable pour chaque bloc extrait ;
- segmentation hiérarchique respectant les sections et les diapositives ;
- indexation et recherche locales sans service externe obligatoire ;
- sélection du contexte pertinent pour les agents GPTMOSS ;
- planification d'un document long ;
- rédaction section par section ;
- contrôle des exigences, affirmations, contradictions, répétitions et références aux sources ;
- assemblage d'un dossier professionnel minimal mais exploitable.

Ne font pas partie de la première livraison : la conversion graphique haute fidélité vers DOCX ou PPTX, le rendu PDF final, l'OCR, la consultation de pages Web distantes et l'installation obligatoire d'un moteur documentaire lourd. Ces fonctions pourront devenir des adaptateurs optionnels sans modifier le contrat commun.

## 3. Architecture cible et intégration

Le flux d'intégration est le suivant :

```text
Fichiers locaux
    -> détection par signature et extension
    -> parseurs sûrs TXT/HTML/DOCX/PPTX
    -> modèle documentaire normalisé
    -> stockage d'artefacts et cache par empreinte
    -> segmentation hiérarchique
    -> index lexical local et recherche
    -> sélection adaptative du contexte
    -> agents de lecture, planification et rédaction
    -> contrôles qualité déterministes
    -> dossier Markdown et rapport de validation
```

### 3.1 Modèle documentaire commun

Chaque fichier produit un `NormalizedDocument` comprenant une empreinte SHA-256, un identifiant déterministe, le nom et le type détecté, le titre éventuel, la version du parseur, une liste ordonnée de blocs et les métadonnées utiles au diagnostic.

Chaque bloc conserve son identifiant, son type, son texte normalisé, sa position logique, son chemin de titres et sa provenance : fichier, numéro de bloc, page ou diapositive lorsque cette notion existe. Ce contrat découple les agents des formats d'origine et accueillera plus tard un parseur PDF ou un moteur optionnel.

### 3.2 Couche de lecture

Le noyau initial repose sur la bibliothèque standard Python :

- TXT/Markdown : encodages usuels, paragraphes et titres ;
- HTML : aucune exécution, suppression du bruit, conservation des titres, listes, tableaux, code et citations ;
- DOCX : archive OOXML locale, ordre du document, styles de titre, paragraphes, listes et tableaux ;
- PPTX : archive OOXML locale, ordre des diapositives, titres, zones de texte et tableaux.

Les parseurs ne chargent aucune ressource référencée et n'exécutent aucun contenu embarqué. Les URL en entrée sont refusées. Les archives sont contrôlées avant lecture : chemins, chiffrement, volume décompressé anormal et ratio de compression extrême.

### 3.3 Stockage, cache et recherche

`ArtifactStore` reste le point d'entrée des téléversements. Il sera étendu pour détecter le format réel, enregistrer la représentation normalisée, mettre l'extraction en cache par empreinte et version, exposer un aperçu lisible et indexer les blocs sans dupliquer les sources.

L'index commence par une recherche hybride légère : termes normalisés, rareté des termes, titres, proximité et position structurelle. Il ne nécessite ni réseau, ni base vectorielle, ni téléchargement de modèle. Une interface permettra ensuite d'ajouter des embeddings locaux.

### 3.4 Contexte adaptatif

Les pièces jointes ne seront plus injectées comme une longue chaîne arbitrairement coupée. Le sélecteur :

1. réserve le budget des instructions et de la réponse ;
2. recherche les blocs liés à la tâche et au plan courant ;
3. conserve les titres et ancêtres nécessaires ;
4. diversifie sources et sections ;
5. adapte le nombre de blocs à la fenêtre disponible ;
6. signale les sources non couvertes.

Les budgets de performance sont calculés à partir du contexte et du corpus. Les limites de sécurité restent configurables et explicites.

### 3.5 Capacités et skills GPTMOSS

Les agents pourront inventorier le corpus, rechercher, lire un bloc avec sa provenance, établir une matrice exigences-sources-sections, planifier, rédiger par section, vérifier couverture et contradictions, puis assembler le livrable et son rapport qualité.

Trois workflows réutilisables sont prévus : analyse de corpus, rédaction professionnelle longue et dossier d'architecture logicielle/système. Ils resteront indépendants du projet de qualification.

## 4. Ordre de développement et de livraison

### Phase A — Socle documentaire

État : terminé et validé sur la branche dédiée.

Livrables : modèle normalisé, registre, détection par contenu, parseurs TXT/HTML/DOCX/PPTX, sérialisation déterministe et rendu Markdown.

Validation : ordre, Unicode, tableaux, titres, diapositives, fichiers trompeurs, URL refusées et archives dangereuses.

Validation exécutée : 12 tests documentaires ciblés et 160 tests GPTMOSS au total réussis avec le runtime Python portable.

### Phase B — Intégration des artefacts

État : terminé et validé sur la branche dédiée.

Livrables : prise en charge dans `ArtifactStore`, API de téléversement/inventaire/aperçu, affichage responsive et compatibilité avec les formats existants.

Validation : tests d'API, non-régression et vérification Edge aux largeurs étroites et larges.

Validation exécutée : 31 tests ciblés réussis, 162 tests GPTMOSS réussis, puis sept scénarios Edge hors ligne sans débordement à 360, 500 et 1440 pixels.

### Phase C — Corpus, cache et recherche locale

État : terminé et validé sur la branche dédiée.

Livrables : segmentation hiérarchique, index persistant reconstruisible, filtres, cache par empreinte/version et diagnostic de couverture.

Validation : corpus multi-format, redémarrage, modification isolée, recherches exactes et thématiques, absence de réseau.

Validation exécutée : recherche du début, du milieu et de la fin, filtres structurels, persistance, invalidation, suppression et reconstruction après corruption ; 170 tests GPTMOSS réussis.

### Phase D — Connexion aux agents

État : terminé et validé sur la branche dédiée.

Livrables : outils de recherche/lecture, injection adaptative, provenance dans les traces et gestion des corpus dépassant la fenêtre du modèle.

Validation : aucun milieu de document perdu silencieusement, sources diversifiées, budget respecté et réponses traçables.

Validation exécutée : portée limitée aux pièces jointes explicites, recherche et pagination avec provenance, relecture complète des chunks, sélection pertinente au milieu d'un gros fichier et échantillonnage début/milieu/fin ; 177 tests GPTMOSS réussis.

### Phase E — Workflows professionnels

État : terminé et validé sur la branche dédiée.

Livrables : skills d'analyse documentaire, de rédaction longue et d'architecture, plan hiérarchique, matrice de couverture, rédaction par section et consolidation.

Validation : contrats génériques, reprise après interruption et résultats intermédiaires persistants.

Validation exécutée : trois skills génériques avec outils documentaires en lecture seule, références locales, matrices de couverture et gates de qualité ; validation officielle `skill-creator`, 18 tests ciblés et 178 tests GPTMOSS réussis.

### Phase F — Contrôles qualité

Livrables : contrôles de structure, couverture, références, sections vides, répétitions, terminologie et affirmations sans source ; rapport JSON et synthèse Markdown ; blocage du statut terminé en cas d'erreur critique.

Validation : défauts injectés, retour ciblé à l'agent responsable, correction puis nouvelle validation.

### Phase G — Documentation et paquet hors ligne

Livrables : documentation utilisateur/développeur, exemples locaux, manifestes et diagnostics d'installation, adaptateurs optionnels non obligatoires.

Validation : installation propre, runtime portable et reproduction depuis la documentation.

### Phase H — Projet réel surveillé

Le projet de qualification sera un dossier d'architecture complet pour une plateforme locale de gouvernance documentaire assistée par IA destinée à une organisation sensible.

Le corpus contiendra réellement :

- un DOCX d'exigences métier et internes ;
- un PPTX de vision, acteurs et contraintes opérationnelles ;
- un TXT de décisions, risques et questions ouvertes ;
- un HTML local décrivant l'existant technique et ses interfaces.

Le livrable minimal comprendra synthèse exécutive, contexte, exigences, architecture logique/données/sécurité/déploiement/exploitation, choix, migration, risques, feuille de route, matrice de traçabilité et rapport qualité.

Le test est accepté seulement si GPTMOSS exécute le workflow complet, couvre les exigences prioritaires, conserve la provenance, ne s'appuie sur aucun lien distant et passe la revue finale.

## 5. Boucle de validation et stratégie Git

Pour chaque incrément :

1. écrire les tests du contrat ou reproduire le défaut ;
2. implémenter le plus petit ensemble cohérent ;
3. exécuter les tests ciblés ;
4. exécuter la suite complète ;
5. examiner le diff et exclure les données de travail ;
6. mettre à jour la documentation concernée ;
7. committer uniquement les fichiers validés ;
8. pousser la branche ;
9. reprendre cette liste et le prochain risque prioritaire.

Le répertoire `workspace/`, les secrets, journaux privés, corpus utilisateur et résultats temporaires ne sont jamais ajoutés. Les jeux de tests sont synthétiques. Un incrément échoué n'est pas poussé avant correction et validation.

## 6. Critères globaux de fin

Le chantier n'est complet que lorsque :

- les quatre formats prioritaires sont détectés et analysés localement ;
- leurs structures et provenances survivent jusqu'au livrable ;
- la recherche retrouve le début, le milieu et la fin de gros fichiers ;
- un corpus plus grand que le contexte est traité sans troncature silencieuse ;
- le workflow reprend après interruption ;
- les contrôles empêchent un livrable incomplet d'être marqué terminé ;
- l'interface reste visible et lisible sur Edge ;
- les tests ciblés, complets et du runtime portable passent ;
- la documentation permet de reproduire le processus ;
- le projet réel aboutit à un dossier utile, cohérent et traçable ;
- tous les incréments validés sont présents sur la branche distante.

Cette liste est la référence de suivi et sera actualisée à chaque phase.
