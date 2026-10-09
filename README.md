# ChamoxisBOT

Bot Discord pour rechercher des torrents via **Prowlarr (Torznab)**, ajouter les téléchargements dans qBittorrent, puis importer vers Plex.

Le nouveau lancement utilise deux applications indépendantes : `manager/` pour Discord,
les événements, la supervision et Ollama ; `modules/plex/` pour les recherches,
téléchargements et imports. Elles communiquent par API HTTP authentifiée.

- [Installation et démarrage Windows](docs/windows.md)
- [Dashboard local et mises à jour par branche](docs/dashboard.md)
- [Architecture, API et événements](docs/architecture.md)
- [Bilan de migration et limites](docs/migration.md)

Double-cliquer sur **`dashboard.bat`** pour ouvrir le panneau local sur
`http://127.0.0.1:8765` : services, téléchargements, recherche, IA, réglages,
logs et mises à jour GitHub avec choix de branche, aperçu et sauvegarde.
Il utilise `.venv-manager`; **`start.bat`** lance les deux applications du bot.

Exemple de nouvelle commande : `/plex demande texte:Ajoute Dune de 2021 en français en 1080p`.
L’IA propose une interprétation, l’utilisateur lance la recherche, sélectionne un résultat,
puis confirme explicitement l’ajout. Les commandes historiques restent disponibles.

Le mode conversation optionnel interprète les messages commençant par `bot` :
`bot, trouve Grey’s Anatomy S11`, `bot pourquoi mon dernier téléchargement est bloqué ?`
ou une simple discussion. L'ajout nécessite toujours une réponse `oui` à la proposition.
Voir le guide Windows pour configurer les salons et activer Message Content Intent.
Les recherches naturelles privilégient MULTI et les seeds positifs, puis le plus
petit torrent compatible avec la qualité demandée (1080p par défaut). L'AV1 et
les torrents à zéro seed annoncé sont exclus; un changement de qualité demande
un accord distinct de la confirmation d'ajout.

Tests automatisés, depuis la racine du dépôt, avec Python 3.12 :

```bash
python -m pip install -c constraints.txt -r manager/requirements.txt -r modules/plex/requirements.txt -r tests/requirements.txt
python -m unittest discover -s tests -v
```

Les sections suivantes décrivent le lancement historique `bot.py`, conservé pour
le retour arrière. Ne pas lancer ce bot en parallèle du gestionnaire central.

## Prérequis
- Python 3.10+
- Bot Discord
- qBittorrent WebUI
- Prowlarr configuré avec C411

## Installation rapide
```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install discord.py aiohttp python-dotenv
cp .env.example .env
```

## Configuration `.env`
Obligatoire:
- `DISCORD_TOKEN`
- `QBIT_URL`, `QBIT_USER`, `QBIT_PASS`
- `PROWLARR_URL`, `PROWLARR_API_KEY`, `PROWLARR_INDEXER_ID`

Optionnel:
- `TORZNAB_RSS_URL` (si tu veux forcer un flux spécifique)
- `TORZNAB_USER_AGENT`
- `TORZNAB_FORCE_UPLOAD`
- `DISABLE_TORRENT_DOWNLOAD` (mode test: sélection OK, mais sans ajout/téléchargement/import)

## Configuration Prowlarr
1. Ajouter l’indexer C411 dans Prowlarr.
2. Récupérer l’API key Prowlarr.
3. Identifier l’ID numérique de l’indexer C411 (`PROWLARR_INDEXER_ID`).

## Lancement
```bash
python bot.py
```

## Commandes (résumé)
- `/recherchetorrent` : recherche via Prowlarr
- `/rssfeed` : lit un flux RSS Torznab et propose un ajout rapide

Le bot utilise uniquement des interactions/slash commands et ne requiert pas le
**Message Content Intent** privilégié. Les suivis longs sont publiés comme des
messages normaux du bot afin de rester modifiables après l'expiration d'une
interaction Discord.

## Dépannage
- Erreur `PROWLARR_API_KEY manquant` : renseigner la variable dans `.env`.
- 401/403: vérifier URL/API key Prowlarr et accessibilité réseau.
- Pas de résultats: vérifier l’indexer C411 dans Prowlarr.
