# Graphe de livraison explicite

Ce document fixe le contrat du graphe de livraison. Il ne décrit pas un
deuxième moteur d'agents et n'autorise pas un graphe de mémoire global.

## Trois couches

1. `dependencies` reste la spine d'ordonnancement : une liste d'identifiants
   `int` ou `str`. Le scheduler n'accepte que cette forme.
2. `plan.edges` porte la sémantique : `produces_for`, `validates`, `repairs`,
   `consolidates`, `blocks`. Le runtime dérive ces arêtes depuis les
   dépendances et les rôles si le planner n'en fournit pas.
3. `evidence_graph` est un extrait borné des lectures corpus, citations et
   couvertures. Il n'expose jamais `tool_call_history` brut.

## Routage des échecs

Un audit ou un quality gate rouvre le propriétaire de l'obligation fautive :

- couverture source absente → inventaire ;
- paragraphe dupliqué ou non sourcé → rédacteur et `replace_paragraph` ;
- défaut logiciel d'intégration ou de commande → debugger déjà prévu ;
- si aucune cible n'est classée, le dernier debugger reste le repli.

Aucune étape n'est insérée pendant l'exécution. Les portes manquantes sont
ajoutées uniquement à la planification par `repair_plan_obligations`.

## Parallélisme

Deux validateurs indépendants peuvent courir ensemble seulement si leurs
`owned_paths` sont disjoints et qu'ils dépendent du même producteur. Le
coordinateur final attend toutes les validations. Les partitions source
restent le parallèle de charge principal.

## GUI

La vue graphe vit dans la colonne plan. Elle ne remplace pas le fil de
conversation et n'ajoute aucune dépendance réseau.
