# Workflow documentaire local

Ce guide explique comment utiliser GPTMOSS pour analyser un corpus local, rédiger un document professionnel long et refuser automatiquement une livraison incomplète. Le contenu est prioritaire ; le livrable initial recommandé est un Markdown simple, lisible et portable.

## Périmètre

Formats pris en charge localement :

| Format | Type de contenu API | Structure conservée |
|---|---|---|
| DOCX | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | titre, titres hiérarchiques, paragraphes, listes et tableaux dans l'ordre du document |
| PPTX | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | ordre et numéro des diapositives, titres, zones de texte et tableaux |
| TXT | `text/plain` | encodages usuels, paragraphes, titres Markdown et listes |
| Markdown | `text/markdown` | titres, paragraphes, listes, tableaux et blocs de code |
| HTML local | `text/html` | titre, titres, texte, listes, tableaux, code et citations, sans script ni chargement de ressource |
| JSON et CSV | `application/json`, `text/csv` | contenu textuel normalisé |
| PDF texte | `application/pdf` | texte par page via `pypdf` ; les pages sans texte sont signalées. Pas d'OCR. |

Un fichier DOCX ou PPTX n'est pas rendu comme dans Office : GPTMOSS en extrait la structure utile au raisonnement. La conversion graphique haute fidélité n'est pas promise.

## Garanties locales et sécurité

Le workflow reçoit uniquement des fichiers téléversés ou déjà présents dans le workspace autorisé. Il ne consulte pas les liens trouvés dans un HTML, un DOCX ou un PPTX, ne charge pas les images distantes et n'exécute pas les scripts intégrés. Une URL fournie à la place d'un chemin local est refusée.

Les fichiers OOXML sont inspectés comme des archives : traversée de chemin, chiffrement, membre anormalement volumineux, volume décompressé excessif et ratio de compression dangereux sont refusés. Ces limites de sécurité restent actives indépendamment des plafonds `max_upload_bytes` et `max_attachment_text_chars` (entiers `≥ 1`).

Les fichiers, représentations normalisées, index et rapports restent sous le workspace local. Seuls les extraits sélectionnés sont envoyés au serveur de modèle configuré. Pour qu'aucun contenu ne quitte la machine, utilisez un serveur de modèle local ou hébergé dans le réseau isolé de l'organisation.

## Démarrage par l'interface

1. Lancez GPTMOSS avec `start.bat` sous Windows ou `./start.sh` sous Linux/macOS.
2. Ouvrez l'interface locale, choisissez le projet cible et ajoutez les fichiers sous la zone de tâche.
3. Ouvrez **Bibliothèque > Documents et images** pour vérifier le nom, le format détecté, le nombre de blocs et l'aperçu normalisé.
4. Cochez uniquement les sources nécessaires à l'exécution. La case d'inventaire automatique impose les obligations corpus au plan, sans réécrire votre texte.
5. Décrivez le public, la décision attendue, les sections obligatoires, les exigences et le niveau de preuve souhaité. Si la case est décochée, votre consigne reste le seul contrat textuel.
6. Sélectionnez au besoin `document-analysis`, `documentation` et `project-architecture`. La sélection automatique les choisit également à partir de la mission.
7. Démarrez l'exécution, surveillez le plan, les références et les validations, puis refusez toute demande d'approbation qui sortirait du workspace prévu.

Exemple de tâche :

```text
À partir des quatre fichiers locaux joints uniquement, rédige un dossier d'architecture
logicielle destiné au comité de décision. Commence par inventorier tout le corpus et
attribue un identifiant stable à chaque exigence. Produis une synthèse exécutive,
le contexte, les exigences, les vues logique/données/sécurité/déploiement/exploitation,
les décisions et alternatives, la migration, les risques, la feuille de route et une
matrice de traçabilité. Référence chaque affirmation matérielle avec le fichier et les
blocs ou la diapositive. N'utilise aucune source Internet. Le livrable principal est
architecture.md ; ajoute quality-policy.json, quality-report.json et quality-report.md.
Ne termine que lorsque la porte qualité document passe.
```

## Dépôt et recherche par API

L'API écoute par défaut sur l'adresse locale `http://127.0.0.1:8000`. Le dépôt utilise du JSON et du Base64 afin que le même contrat fonctionne pour tous les formats.

### Déposer les sources

```powershell
function Send-GptmossDocument {
  param([string]$Path, [string]$ContentType)
  $item = Get-Item -LiteralPath $Path
  $body = @{
    filename = $item.Name
    content_type = $ContentType
    content_base64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($item.FullName))
  } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/artifacts' `
    -ContentType 'application/json' -Body $body
}

$requirements = Send-GptmossDocument '.\sources\requirements.docx' `
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
$vision = Send-GptmossDocument '.\sources\vision.pptx' `
  'application/vnd.openxmlformats-officedocument.presentationml.presentation'
$decisions = Send-GptmossDocument '.\sources\decisions.txt' 'text/plain'
$existing = Send-GptmossDocument '.\sources\existing.html' 'text/html'
```

La réponse fournit notamment `id`, `filename`, `content_type`, `sha256`, `document_blocks`, `document_chunks`, `document_parser` et `document_parser_version`. GPTMOSS détecte aussi le contenu réel : renommer un binaire avec une extension trompeuse ne contourne pas le parseur.

### Inventorier, prévisualiser et rechercher

```powershell
# Inventaire
Invoke-RestMethod 'http://127.0.0.1:8000/artifacts'

# Aperçu normalisé
Invoke-RestMethod "http://127.0.0.1:8000/artifacts/$($requirements.id)/preview"

# Recherche locale dans tout le corpus
$query = [uri]::EscapeDataString('rétention chiffrement reprise activité')
Invoke-RestMethod "http://127.0.0.1:8000/artifacts/search?q=$query&limit=12"

# Recherche limitée à deux pièces jointes
Invoke-RestMethod (
  "http://127.0.0.1:8000/artifacts/search?q=$query&limit=12" +
  "&artifact_id=$($requirements.id)&artifact_id=$($vision.id)"
)
```

La recherche peut aussi filtrer `content_type`, `heading` et `kind`. Chaque résultat contient son score, le fichier, le chemin de titres, le type de bloc ou chunk et sa provenance.

### Lancer l'exécution avec une portée explicite

```powershell
$request = @{
  task = 'Produis le dossier d architecture décrit dans la mission, avec traçabilité et rapports qualité.'
  project_id = 'proj-default'
  attachment_ids = @($requirements.id, $vision.id, $decisions.id, $existing.id)
  agent_config = @{
    skills = @('document-analysis', 'documentation', 'project-architecture')
  }
} | ConvertTo-Json -Depth 6

$execution = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/executions' `
  -ContentType 'application/json' -Body $request
```

La capability `documents` ne peut inventorier, rechercher et lire que les identifiants présents dans `attachment_ids`. Une autre exécution ne gagne donc pas implicitement accès à tout le corpus stocké. `documents.read` et le filtre de `documents.search` acceptent l'`artifact_id` recommandé, mais aussi le nom de fichier exact ou l'empreinte `document_id` renvoyés par l'inventaire. Ces alias sont résolus uniquement parmi les pièces jointes de l'exécution ; un identifiant inventé, ambigu ou appartenant à un autre corpus reste refusé et l'erreur rappelle les références autorisées.

L'inventaire distingue explicitement deux systèmes de coordonnées. `normalized_block_offsets` décrit les offsets à base zéro attendus par `documents.read start_block`. `citation_bounds` décrit les bornes à base un autorisées dans les références du livrable. Pour un PPTX, ces bornes utilisent les numéros de diapositives déclarés par le document, y compris une diapositive sans bloc textuel ; elles ne doivent jamais être déduites du nombre ou de l'ordre des blocs normalisés.

## Fonctionnement interne

```text
fichier local
  -> détection par signature et extension
  -> normalisation en blocs avec provenance
  -> cache par empreinte et version du parseur
  -> chunks hiérarchiques
  -> index lexical persistant local
  -> recherche et sélection adaptative
  -> agents d'analyse, architecture et rédaction
  -> constats QA, réparation et rapports qualité
  -> audit déterministe indépendant
  -> livrable Markdown et assurance finale
```

Le fallback professionnel estime désormais un budget d'étapes selon la taille réelle du corpus et du livrable. Une note courte reçoit un chemin compact ; un dossier à nombreuses sources, exigences, rapports ou diagrammes conserve les étapes d'analyse, rédaction, réparation et assurance indépendante. La production de la politique et des rapports qualité appartient à un rédacteur de preuves après réparation ; un QA distinct crée ensuite `analysis/final-delivery-audit.md` sans modifier les sorties des auteurs, puis le coordinateur contrôle toute la traçabilité. Les exigences complètes transmises à un spécialiste restent limitées à sa nature de travail et ne l'autorisent jamais à écrire les livrables des étapes sœurs.

Le plan LLM décrit des opérations et leurs dépendances, sans minimum global d'étapes. La
politique `corpus_policy` impose les garanties locales sans devenir une nouvelle demande
utilisateur. Les champs `operation`, `satisfies_obligations` et `required_evidence` sont
contrôlés causalement ; un fallback incomplet est réparé avant exécution. Le compilateur
mesure documents, images, octets, blocs, chunks, formats et erreurs. Lorsque la charge le
justifie, il remplace uniquement l'opération source par des partitions stables (borne de
persistance 128) puis une consolidation. Chaque pièce appartient à une seule partition ;
le consolidateur reçoit leurs résultats validés et non une nouvelle copie du corpus. Le
scheduler conserve toutes ces unités mais n'en exécute qu'une vague bornée simultanément,
automatique avec `max_parallel_plan_steps=0`.

Les arêtes typées (`plan.edges`) expliquent pourquoi une étape dépend d'une autre
(`produces_for`, `validates`, `repairs`, `consolidates`, `blocks`) sans changer
l'ordonnancement. Un quality gate documentaire classifie le défaut et rouvre le
propriétaire : inventaire si la couverture source manque, rédacteur avec
`replace_paragraph` pour un doublon ou un paragraphe non sourcé, debugger seulement
pour un défaut logiciel. `GET /executions/{id}/evidence-graph` projette inventaires,
lectures, images et citations en un graphe borné unifié par SHA-256 ; ce n'est pas
un dump de `tool_call_history`. Dans la GUI, l'onglet **Graphe** de la colonne plan
dessine cette topologie localement ; **Liste** reste la vue détaillée.

L'identité canonique d'une source vient toujours du stockage d'artefacts. Pour un dossier,
le chemin relatif complet remplace donc les éventuels basenames proposés par le planner
dans toutes les politiques documentaires fondées sur les sources ; inventaire, analyses
intermédiaires et livrable final valident ainsi les mêmes citations.

Pour un document long, le moteur sectionnel crée un contrat stable par titre (objectif,
volume cible, exigences, preuves et dépendances), sauvegarde chaque révision dans
`.gptmoss/document-state` et réassemble le Markdown uniquement à partir des sections
validées. Cette mémoire réduit les répétitions et permet de reprendre après une
interruption fournisseur sans recommencer les sections terminées.

Les blocs `mermaid` ou `diagram` sont convertis par le modèle canonique de diagramme,
contrôlés (nœuds, arêtes, zones de confiance, densité, métadonnées), puis rendus en
SVG déterministe. Le paquet DOCX conserve la figure dans `word/media/` et relie son
`rId` dans `document.xml`; une erreur sémantique n'est jamais masquée par un dessin vide.

L'index est lexical, accent-insensible et sans modèle à télécharger. Il est enregistré dans `workspace/uploads/document-index.json`, rechargé au redémarrage et reconstruit automatiquement si son état ne correspond plus aux documents. La représentation normalisée de chaque fichier évite de reparcourir l'archive à chaque lecture.

Quand le corpus dépasse la fenêtre du modèle, GPTMOSS n'injecte pas simplement le début du premier fichier. Il recherche les passages liés à la tâche, conserve leurs titres, diversifie les sources et, sans requête assez discriminante, échantillonne le début, le milieu et la fin. Les agents paginent `documents.inventory`, relisent le texte avec `documents.search`, `documents.read` et `documents.read_chunk`, puis demandent les images précises avec `documents.read_image` ou par lots de quatre avec `documents.read_images`. Une image n'est comptée comme analysée qu'après une complétion multimodale réussie. Si une étape promet un inventaire intégral ou exhaustif, son handoff est refusé tant que l'historique ne prouve pas la lecture de chaque bloc et la présentation de chaque image de la partition ; le nombre de diapositives ne peut donc pas être confondu avec le nombre de blocs PPTX. Sa politique `require_source_coverage` vérifie séparément que l'union des plages citées couvre chaque bloc ou diapositive déclaré dans `source_inventory` : avoir lu une source sans documenter sa couverture ne suffit pas.

Si le fournisseur renvoie un plan invalide ou trop petit, le fallback déterministe reconnaît une mission documentaire à partir des formats, pièces jointes, actions `documents` et objectifs de rédaction. Il conserve les noms de livrables explicitement listés, reconstruit la politique `document`, sépare analyse du corpus, exigences, décisions, architecture, sécurité, SRE, migration, rédaction, QA, réparation et audit final. Un nom comme `vision.pptx` ou le verbe « porter » dans « porter une référence » n'est pas interprété comme un projet de computer vision ou de vêtement numérique.

Avec `adaptive_resource_management=true`, `max_context_chars` est un plancher d'historique. `context_window_tokens=0` active la découverte prudente de la fenêtre du backend et `context_output_reserve_tokens` préserve la place de réponse. `max_upload_bytes` et `max_attachment_text_chars` sont des plafonds applicatifs stricts (`≥ 1`) ; la valeur `0` est refusée. Le préflight du modèle reste le plafond final en plus de ces limites, pas à leur place.

## Provenance attendue

Une référence locale machine-vérifiable suit l'une de ces formes :

```text
[requirements.docx > Sécurité > Gestion des accès > blocks 18-21]
[vision.pptx > slide 4]
```

La référence doit rester au plus près du paragraphe qu'elle soutient. Une recommandation, une hypothèse ou une inférence doit être nommée comme telle lorsqu'elle n'est pas directement imposée par une source.

Pour une revue exhaustive, l'agent tient une matrice contenant au minimum : identifiant, exigence ou affirmation, fichier, chemin de titres, plage de blocs ou diapositive, synthèse de preuve, confiance, contradiction ou manque, et section du livrable.

## Politique qualité déclarative

Le plan peut déclarer un validateur `document` dans `artifact_validations`. Chaque politique est appliquée immédiatement à l'artefact produit avant tout handoff, puis lors de l'audit final. Le fallback de rédaction professionnelle crée aussi des politiques pour l'inventaire, les matrices, les analyses spécialisées, les registres et les rapports JSON/Markdown : un simple fichier non vide ne suffit donc plus. Les locators bornés acceptent `block/blocks`, `bloc/blocs`, `slide` et `diapositive`, sans changer les bornes numériques exigées. La même politique peut être enregistrée dans `quality-policy.json` :

```json
{
  "required_headings": [
    "Synthèse exécutive",
    "Contexte et périmètre",
    "Exigences",
    "Architecture logique",
    "Données",
    "Sécurité",
    "Déploiement et exploitation",
    "Migration",
    "Risques",
    "Feuille de route",
    "Matrice de traçabilité"
  ],
  "min_section_words": 30,
  "required_requirement_ids": ["REQ-001", "REQ-002", "NFR-001"],
  "required_traceability_ids": ["REQ-001", "REQ-002", "NFR-001"],
  "required_source_files": [
    "requirements.docx",
    "vision.pptx",
    "decisions.txt",
    "existing.html"
  ],
  "source_inventory": {
    "requirements.docx": {"blocks": 84},
    "vision.pptx": {"slides": 12},
    "decisions.txt": {"blocks": 31},
    "existing.html": {"blocks": 48}
  },
  "require_local_references": true,
  "require_bounded_references": true,
  "require_claim_references": true,
  "claim_min_words": 24,
  "forbid_external_links": true,
  "forbid_placeholders": true,
  "max_duplicate_paragraphs": 0,
  "duplicate_min_words": 14,
  "terminology": {
    "fournisseur d'identité": ["serveur d'identité", "IdP legacy"]
  },
  "minimums": {"words": 2500, "local_references": 20},
  "maximums": {"external_links": 0, "placeholder_markers": 0}
}
```

Renseignez les bornes depuis le corpus réel ; ne recopiez pas les nombres de cet exemple. `required_traceability_ids` exige que chaque identifiant apparaisse dans une ligne de tableau Markdown, pas seulement quelque part dans le texte. `terminology` associe le terme canonique aux variantes interdites.

Métriques disponibles : `characters`, `words`, `lines`, `headings`, `paragraphs`, `local_references`, `cited_sources`, `external_links`, `placeholder_markers`, `duplicate_paragraphs`, `unsupported_claim_paragraphs` et les compteurs de couverture des titres, exigences, lignes de traçabilité et sources.

## Générer et lire les rapports

Depuis la racine du dépôt :

```powershell
python scripts/validate_document.py .\workspace\projects\proj-default\architecture.md `
  --constraints .\workspace\projects\proj-default\quality-policy.json `
  --json .\workspace\projects\proj-default\quality-report.json `
  --markdown .\workspace\projects\proj-default\quality-report.md
```

Le script renvoie `0` si le document passe et `1` si une règle échoue. Il fonctionne avec le Python portable isolé fourni par GPTMOSS. Le JSON est la preuve machine complète ; le Markdown est une synthèse lisible des métriques, erreurs et avertissements.

Dans une exécution GPTMOSS, la porte de livraison appelle automatiquement le même validateur lorsque le plan déclare l'artefact. Un fichier manquant, vide ou invalide empêche `delivery_assurance.passed` de devenir vrai et interdit au coordinateur de présenter la livraison comme terminée.

## Vérification de l'installation

```powershell
# Point d'entrée documentaire sous le runtime sélectionné
python scripts/validate_document.py --help

# Tests des parseurs, de la recherche, des agents et du validateur
python -m pytest -q tests/test_documents.py tests/test_corpus.py `
  tests/test_document_capability.py tests/test_document_quality.py `
  tests/test_long_document_engine.py tests/test_diagrams_and_docx.py

# Suite complète GPTMOSS
python -m pytest -q
```

DOCX et PPTX sont lus avec XML et ZIP de la bibliothèque standard. Le texte PDF utilise `pypdf`, déjà épinglé dans `requirements-runtime.txt` et le runtime portable. Les tests du paquet vérifient que le Python portable versionné exécute le point d'entrée documentaire.

## Diagnostic

| Symptôme | Vérification et correction |
|---|---|
| DOCX ou PPTX refusé | Vérifier qu'il s'agit d'un OOXML non chiffré et non d'un ancien `.doc` ou `.ppt` renommé. |
| HTML presque vide | Le contenu utile doit exister dans le fichier ; les scripts, styles et ressources distantes sont volontairement ignorés. |
| Recherche sans résultat | Vérifier l'inventaire, essayer un synonyme, retirer les filtres et confirmer que l'ID est joint à l'exécution. |
| Source absente du contexte | Ajouter son ID dans `attachment_ids` ; la présence en bibliothèque ne suffit pas. |
| Référence hors bornes | Relire le bloc ou la diapositive par la capability `documents`, puis corriger la référence ou l'inventaire. |
| Section signalée vide | Un titre suivi uniquement d'un placeholder n'est pas du contenu ; rédiger puis relancer le validateur. |
| Trop de paragraphes non sourcés | Ajouter une référence locale proche ou marquer explicitement recommandation, hypothèse ou inférence et adapter la politique si nécessaire. |
| Index endommagé après arrêt brutal | Redémarrer GPTMOSS ; `ArtifactStore` compare les empreintes et reconstruit l'index local. |
| Commande Python introuvable | Lancer `install.bat`, puis utiliser `start.bat` et le runtime détecté ; ne pas copier seulement une partie du paquet autonome. |

## Limites connues

- aucune fidélité de mise en page Office n'est promise ; le contenu structuré est la priorité ;
- les notes de présentateur, macros, objets OLE, contenus audio/vidéo et images sans texte ne sont pas interprétés ;
- aucun OCR n'est effectué ;
- la recherche lexicale ne remplace pas un modèle sémantique, mais évite réseau, poids et dépendances ;
- le contrôle des affirmations sans source est heuristique et configurable ; la revue humaine reste nécessaire pour une décision critique ;
- la validité structurelle et la traçabilité ne prouvent pas à elles seules la justesse métier de chaque décision.

Le contrat vivant est ce guide, [architecture.md](architecture.md) et [delivery-graph.md](delivery-graph.md). [document-workflow-plan.md](document-workflow-plan.md) est un journal historique, plus le tracker d'intégration.
