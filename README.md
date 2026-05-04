# ChamoxisBOT

Bot Discord pour rechercher des torrents via **Prowlarr (Torznab)**, ajouter les téléchargements dans qBittorrent, puis importer vers Plex.

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

## Dépannage
- Erreur `PROWLARR_API_KEY manquant` : renseigner la variable dans `.env`.
- 401/403: vérifier URL/API key Prowlarr et accessibilité réseau.
- Pas de résultats: vérifier l’indexer C411 dans Prowlarr.
