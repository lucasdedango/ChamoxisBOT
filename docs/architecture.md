# Applications, API et événements

`manager/` héberge un seul bot Discord, les notifications, le registre de
services, la supervision et la passerelle Ollama. `modules/plex/` héberge les
opérations métier. Aucune application n’importe les classes internes de l’autre.
`shared/schemas.py` contient uniquement les contrats Pydantic versionnés.
`chamoxis_common/` contient l’infrastructure réutilisable : SQLite, transport,
authentification et journalisation, sans métier Plex ni Discord.

Les points d’entrée sont `python -m manager.main` et
`python -m modules.plex.main`, exécutés depuis la racine du checkout. Chaque
application dispose de ses dépendances et de son `.env`. Pour une séparation
future, distribuer `shared` et `chamoxis_common` comme un petit paquet commun,
ou remplacer les modèles par des clients générés depuis OpenAPI. Il n’est pas
nécessaire de conserver des chemins absolus vers l’autre application.

## API v1

Les corps JSON sont validés et les champs inconnus refusés. Les API métier
exigent `X-API-Key` ; l’absence de clé configurée empêche leur lancement.
`/docs` et `/openapi.json` décrivent les contrats techniques et ne contiennent
pas de valeurs de configuration. Ils restent locaux par défaut.

| Gestionnaire | Fonction |
| --- | --- |
| `GET /health` | Santé de l’API et état de la connexion Discord |
| `GET /services` | État des services configurés |
| `GET /services/{id}/status` | État d’un service |
| `POST /services/{id}/restart` | Clé administrative distincte requise, service explicitement géré |
| `POST /ai/chat` | `messages: [{role, content}]`, `structured: bool` ; retourne `backend`, `model`, `result` |
| `POST /ai/analyze` | `text` ; retourne `backend`, `model`, `intent` validé |
| `POST /events` | Acceptation durable et déduplication d’un événement |
| `POST /notifications` | `request_id`, `channel_id`, `text` ; salons autorisés seulement |
| `POST /modules/register` | `id`, `url` ; module préalablement déclaré dans le registre |

| Module Plex | Fonction |
| --- | --- |
| `GET /health` | Santé du module, sans promesse sur les services externes |
| `GET /config` | Indexers et drapeaux non secrets |
| `POST /search` | `query`, `indexer`, `quality`, `language`, `limit` ; `rank_preferences`, `year`, `season`, `episode` pour classer une recherche large |
| `GET /rss` | `url` optionnelle, `limit` ; flux configuré par défaut |
| `GET /torrents` | Liste qBittorrent, `limit` |
| `GET /torrents/{hash}` | État qBittorrent, ou null si absent |
| `POST /downloads` | Enregistrement durable d’une demande confirmée, réponse 202 |
| `GET /tasks` | Demandes et états persistés |
| `GET /tasks/{id}` | Suivi d’une demande |
| `POST /tasks/{id}/cancel` | Annule un suivi autorisé par son état ; ne supprime rien dans qBittorrent |
| `GET /storage` | Racines, espace libre et suggestions (`query`, `kind`) |
| `GET /library` | Correspondances approximatives de dossiers (`title`, `kind`) |

Une demande d’ajout contient `request_id`, `link`, `title`, `user_id`,
`channel_id`, `prefs` et **`confirmed: true`**. `prefs` contient le type,
le nom de dossier, le mode série et la saison/épisode. Le nom cible ne peut pas
être un chemin. Les réponses de recherche remplacent les URL authentifiées
des indexers par une référence opaque `result:…`, résolue uniquement dans le
module Plex. Un lien direct ou magnet reste possible.

Un même `request_id` et les mêmes paramètres retournent la même tâche. Des
paramètres différents avec cet identifiant donnent 409. Cette garantie porte
sur l’enregistrement de la demande ; qBittorrent et SQLite ne partagent pas
de transaction atomique.

## Tâches et livraison

États usuels : `queued → adding → downloading → importing → completed`.
Autres états : `simulated`, `duplicate`, `failed`, `cancelled`, `needs_review`.
Les téléchargements reprennent leur suivi après redémarrage. Un arrêt au milieu
d’un ajout ou d’un import mène à `needs_review` : inspecter qBittorrent et les
fichiers avant toute nouvelle confirmation. Aucune opération incertaine n’est
rejouée automatiquement. Les imports sont séquentiels pour préserver les
opérations de renommage et déplacement historiques.

Un événement suit le contrat suivant :

```json
{
  "version": 1,
  "id": "identifiant-unique",
  "source": "plex",
  "type": "plex.import.completed",
  "task_id": "identifiant-de-tache",
  "channel_id": 123,
  "user_id": 42,
  "data": {"message": "Import terminé", "moved_files": 1}
}
```

Le module publie `module.started`, `module.stopped`, `torrent.added`,
`torrent.progress`, `torrent.completed`, `torrent.failed`, `torrent.simulated`,
`plex.import.started`, `plex.import.completed`, `plex.import.failed`.
Le gestionnaire publie `service.changed`. Les événements du module sont
conservés dans une outbox SQLite et renvoyés avec backoff tant que le
gestionnaire ne les a pas acceptés. Le gestionnaire les conserve avant
d’accuser réception ; leur identifiant déduplique les réceptions suivantes.

Les notifications sont également reprises après reconnexion Discord. Les
messages par tâche sont réédités grâce à leur identifiant persisté. Une panne
exactement entre l’envoi Discord et son enregistrement peut néanmoins produire
une notification doublonnée : la livraison externe n’est pas « exactement une
fois ». Les salons non autorisés sont refusés et les mentions sont désactivées.
Configurer les salons avant de créer des demandes dont on attend une notification.

## Futurs modules et outils IA

Un module ShopWatcher devra disposer de son propre processus, exposer une
sonde de santé, être déclaré dans `services.json`, puis utiliser la clé normale
du gestionnaire pour `/modules/register`, `/events`, `/notifications` et
`/ai/chat`. Son code de recherche d’annonces n’est pas inclus dans cette tâche.
Les nouveaux adaptateurs n’ajoutent aucune commande système à l’API.

`manager/ai/tools.py` prépare un registre d’outils explicites avec schéma de
paramètres, contrôle d’utilisateur et confirmation. Aucun outil n’est activé
et aucune sortie IA n’est exécutée. Le parcours actuel interprète seulement
une demande, puis utilise les menus Discord et l’API métier habituelle.
