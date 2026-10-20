# Diagnostic des recherches et imports longs

## Résultats de recherche

Chaque recherche affiche dans les logs du module Plex :

- `Indexer page` : indexer, offset, résultats demandés/reçus et stratégie de recherche complète (`refined=True`).
- `Search excluded` : titre, indexer, seeds et motifs d'exclusion, sur au plus 50 torrents par recherche.
- `Search exclusions counts` : total par motif, même au-delà des 50 lignes détaillées.
- `Search results` : résultats reçus, sans AV1, retenus et erreurs d'indexer.

Les codes sont `av1`, `title`, `media_kind`, `year`, `season`, `episode`, `season_pack`, `quality`, `language` et `seed_preference`. `media_kind` écarte les saisons/épisodes lorsque la demande porte sur un film. `season_pack` signifie que le résultat ne correspond pas à une saison complète demandée. Un torrent peut avoir plusieurs motifs : les totaux ne s'additionnent pas comme des résultats uniques. `seed_preference` signifie que le résultat est passé derrière d'autres candidats annonçant des seeds positifs ou le minimum demandé, pas que tous les torrents à zéro seed sont interdits.

Sans résultat, la conversation indique les motifs d'exclusion. Les filtres et le choix de qualité restent ceux de la demande. L'absence d'année dans un titre reste un avertissement, pas une exclusion automatique. Le diagnostic ne certifie pas l'identité d'une œuvre dont le titre reste ambigu.

## Déplacements entre disques

Avant l’import, le module attend aussi la fin du déplacement automatique qBittorrent de `incoming` vers `complete` et des vérifications en cours. Il ne renomme pas les fichiers pendant ces opérations.

Le module attend tant que qBittorrent annonce `moving`, même au-delà de cinq minutes ou de trente minutes. Il écrit `Torrent move pending` environ toutes les minutes avec le temps écoulé. La fin n'est confirmée que lorsque qBittorrent n'annonce plus de déplacement **et** expose un chemin correspondant à la destination demandée.

Si qBittorrent cesse d'annoncer `moving` sans confirmer la destination, le délai est de trente minutes depuis la dernière observation `moving` (ou depuis la demande si aucun déplacement n'a été observé). Une erreur explicite `error`/`missingFiles`, un torrent supprimé ou une indisponibilité persistante entraînent une vérification manuelle. Une coupure temporaire de consultation est réessayée sans renvoyer `setLocation`.

Le suivi peut traiter jusqu'à quatre téléchargements/imports en parallèle. Les nouveaux ajouts restent sérialisés pour identifier correctement le torrent ajouté. Un import long ne bloque plus tous les ajouts suivants. Si les quatre suivis sont occupés par des imports, les autres suivis attendent une place. Un redémarrage pendant un import conserve la protection existante : l'import interrompu passe en `needs_review`, sans rejouer automatiquement les renommages/déplacements.

Les tâches déjà en `needs_review` ne sont pas relancées par la mise à jour. Vérifier l'état et l'emplacement réels dans qBittorrent avant toute intervention; un torrent déjà téléchargé n'a pas à être téléchargé à nouveau.

## Discord

`/recherchetorrent` et `/info` accusent réception immédiatement, puis modifient la réponse différée. `/plex recherchetorrent` utilise le même callback. Une interaction déjà expirée (`10062` ou `10015`) est journalisée une seule fois sans essayer de lui envoyer une seconde réponse. Il faut relancer la commande. Une coupure réseau ou un blocage de la boucle Discord peut toujours empêcher l'accusé de réception d'arriver à temps.

## Confidentialité des logs

Les erreurs de déplacement n'incluent plus le dictionnaire complet du torrent, qui peut contenir les URL privées des trackers. Le masquage couvre les passkeys dans les chemins `/announce/<passkey>`, `/<passkey>/announce`, les paramètres `passkey`/`authkey` et les URL encodées dans les magnets, y compris dans les traces d'exception JSON. Cela ne modifie pas les anciens fichiers de logs déjà écrits.

## Installation et validation

Mettre à jour `test/manager-plex-ollama`, puis redémarrer le gestionnaire et le module Plex. Aucun nouveau champ `.env` ni paquet Python n'est requis par cette correction.

Les tests simulent des déplacements longs, les erreurs et reprises de consultation, l'ajout pendant un import, l'expiration Discord et les exclusions Prowlarr. La vitesse et les permissions de vos disques Windows restent à vérifier sur la machine réelle.
