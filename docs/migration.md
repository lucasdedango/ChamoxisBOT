# Bilan et validation

## Fonctionnalités transférées

- Client qBittorrent et ses mécanismes de connexion/reconnexion : `core/torrents.py`.
- Flux Torznab, métadonnées, téléchargements `.torrent` et filtres : `core/search.py`.
- Choix des volumes et contrôle de l’espace libre : `core/storage.py`.
- Reconnaissance films/séries, renommage résilient, attente des déplacements,
  imports et rafraîchissement Plex : `core/engine.py`.
- Suivi autonome, état des tâches et événements persistants : `core/downloads.py`.
- Menus, boutons, choix du dossier, qualité, saison et mode série : `manager/discord/bot.py`.

Le moteur d’import reste volontairement regroupé pendant cette première
extraction afin de conserver les interactions entre renommage, déplacement et
chemins qBittorrent. Il pourra être subdivisé davantage après validation terrain.
`bot.py` et ses neuf tests historiques restent conservés pour comparer et revenir
au fonctionnement précédent. Le nouveau gestionnaire ne charge pas `bot.py`.

## Changements visibles assumés

Les commandes historiques et leurs équivalents `/plex …` coexistent. Un résultat
sélectionné demande maintenant une confirmation supplémentaire avant l’ajout.
`/plex demande` interprète une phrase puis propose une recherche et le même
parcours de sélection/confirmation. `/plex operations` affiche les demandes
de l’utilisateur ; les administrateurs peuvent consulter toutes les demandes.

Les mises à jour se font par événements et message de tâche persistant, avec
progression par tranches de 10 %, plutôt que par une boucle Discord. Les imports
continuent lorsque Discord ou le gestionnaire sont indisponibles. Les événements
terminaux restent consultables même après reconnexion.

`/cleartorrents` est réservé aux administrateurs et annule les suivis persistants
annulables. Il ne supprime ni torrent ni fichier, et n’interrompt pas un import
déjà commencé. La résolution ambiguë d’un nouveau torrent ne choisit plus
automatiquement le dernier torrent de la catégorie : elle demande une vérification.

## Vérifications et limites

La suite teste le fonctionnement historique, le moteur extrait, les contrats,
l’authentification, les permissions, l’idempotence, les pannes de suivi, la
persistance, la livraison des événements et les limites de relance. Elle inclut
des échanges réseau HTTP réels entre les deux API et des doubles locaux de
Prowlarr/Ollama. Aucun test n’utilise les services personnels ni un vrai torrent.
Une CI Linux/Windows est ajoutée ; sa présence ne signifie pas qu’un run distant
a déjà été exécuté.

Restent à valider sur les machines cibles : les interactions Discord complètes,
les API et versions exactes de Prowlarr/qBittorrent/Plex, les imports de films et
séries avec les chemins Windows réels, le contrôle SCM, les tâches planifiées,
la quantification des modèles et leur coexistence avec le transcodage Plex.
Les paramètres Windows de processus et services sont configurables ; les
scripts n’ont pas été exécutés sous Windows dans le cloud.

La détection de contenu déjà présent utilise actuellement les noms de dossiers,
avec une indication explicite de correspondance approximative. Ce n’est pas
encore une recherche dans le catalogue Plex par identifiant de film.
La langue est filtrée sur les tags de titre des indexers et ne garantit pas les
pistes audio du fichier. L’utilisateur doit vérifier le résultat proposé.

Les boîtes d’événements, références de recherche et tâches sont conservées sans
purge automatique dans cette version. Sauvegarder les dossiers `data` et prévoir
une politique de rétention après validation ; ils peuvent contenir des liens
authentifiés et doivent rester privés. Les clés de module donnent accès à leur
API locale : ne les distribuer qu’à des modules de confiance. Une isolation
par module et un durcissement réseau supplémentaire seront nécessaires pour
des modules non fiables ou des API exposées hors du PC.
