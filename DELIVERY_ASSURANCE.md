# Garantie de livraison GPTMOSS

Cette couche empêche qu'un projet soit déclaré terminé sur la seule appréciation
du LLM ou sur une suite de tests superficielle.

## Contrat créé avant l'exécution

Le coordinateur fige dans `variables.delivery_contract` :

- le SHA-256 du prompt initial et du contrat ;
- les exigences utilisateur obligatoires (`REQ-xxx`) ;
- la matrice exigence → implémentation → validation indépendante ;
- les changements de périmètre proposés ;
- les chemins possédés par chaque spécialiste ;
- les interfaces publiques, commandes de vérification et commandes de lancement.

Les plans anciens sans ces champs sont enrichis de façon déterministe et hors
ligne. Les nouveaux plans doivent déclarer `requirements`, `scope_changes`,
`interfaces`, `launch_commands`, puis `requirement_ids` et `owned_paths` dans
chaque étape.

## Réduction de périmètre

Une limite MVP, une fonctionnalité différée, un mock remplaçant une fonction
obligatoire ou un élément placé hors périmètre produit
`pending_scope_approval`. L'exécution passe à `paused` avant toute modification
du projet.

- `POST /executions/{id}/approve` accepte le nouveau périmètre et reprend ;
- `POST /executions/{id}/reject` refuse la réduction et termine en échec honnête ;
- `POST /executions/{id}/resume` ne contourne jamais cette décision.

La décision, son motif, l'horodatage et le hash du contrat sont persistés.

## Coordination sans doublons

Un spécialiste reçoit les livraisons validées de ses dépendances et ne doit pas
les refaire. Les mutations `filesystem.write` et `filesystem.delete` sont
comparées à ses `owned_paths`. Deux écritures concurrentes sur le même chemin
sont sérialisées. Seul un réparateur peut reprendre un fichier appartenant à un
autre lot ; le contrat interne `.gptmoss/**` reste protégé.

Une modification de contenu ne renouvelle plus indéfiniment le budget d'une
étape. Le moteur reconnaît comme delta qualité :

- un nouvel artefact ;
- un artefact obligatoire enfin créé ;
- une nouvelle commande de vérification réussie ;
- une diminution du nombre d'échecs observés ;
- au maximum deux modifications préparatoires par fichier sans autre progrès.

Après stagnation, un nouveau spécialiste reçoit les erreurs machines et reprend
le workspace existant.

Une reprise manuelle d'une exécution principale en échec remet à zéro uniquement
le runtime de l'étape défaillante. Elle conserve le plan, les artefacts, les preuves
et les étapes terminées. Les pauses d'approbation et les réductions de périmètre
restent soumises à `/approve` ou `/reject` et ne peuvent pas être contournées par
`/resume`.

## Audit indépendant final

Avant `completed`, GPTMOSS évalue lui-même :

1. la couverture de chaque exigence obligatoire ;
2. la présence et la taille des artefacts ;
3. la syntaxe Python et l'identité canonique des paquets ;
4. les appels dont les arguments contredisent les signatures réelles ;
5. les interfaces publiques figées (`module`, `symbol`, `parameters`,
   `consumers`) ;
6. l'exécution exacte et réussie des commandes QA indépendantes ;
7. l'exécution réelle des commandes de lancement CLI/API prévues.

Le rapport est disponible dans `results.delivery_assurance` et dans l'interface.
S'il échoue, le dernier réparateur est rouvert avec le rapport exact, puis
l'auditeur final est rejoué. Les lots déjà validés ne sont pas relancés. Après
épuisement des reprises, le projet passe à `failed` au lieu de produire une
fausse réussite.

Le coordinateur évalue les commandes obligatoires à partir de son historique et
de celui de tous ses sous-agents. Une validation QA exacte et réussie reste donc
utilisable après délégation ou redémarrage. Si un coordinateur terminal repris a
déjà un runtime persistant, que toutes les autres étapes sont `completed`, que ses
gates ne signalent plus rien et que le rapport indépendant passe, le moteur produit
la livraison finale avant un nouvel appel LLM. Cette clôture déterministe est
désactivée pour une première exécution et lorsqu'une approbation est encore en
attente.

Les tests E2E doivent lancer les points d'entrée publics depuis un processus
frais et utiliser des fixtures locales. Cette garantie complète les tests du
projet généré ; elle ne prouve pas à elle seule une propriété métier impossible
à mesurer automatiquement.

## Panne du fournisseur LLM

Après une courte rafale de tentatives, une erreur réseau temporaire place
l'exécution dans `waiting_provider`. Le plan, les conversations, les artefacts,
les affectations et les preuves restent dans `state_store.json`.

GPTMOSS retente avec un backoff plafonné à cinq minutes. Au redémarrage de
l'API, les exécutions dans cet état sont automatiquement reprogrammées.
`POST /executions/{id}/resume` permet également une tentative immédiate.
Les erreurs permanentes (clé invalide, 401, 403) restent des échecs immédiats.

## Benchmark hors ligne

Le benchmark couvre actuellement six demandes complexes de domaines différents
(3D, inventaire, documents privés, pipeline de données, incidents et inspection
industrielle) :

```powershell
python scripts/run_delivery_benchmarks.py
```

Il échoue si un plan complexe est sous-dimensionné, réutilise trop de profils
génériques, oublie la réparation autonome ou l'auditeur final, laisse une
exigence sans implémentation/validation, ou crée un artefact sans propriétaire.
