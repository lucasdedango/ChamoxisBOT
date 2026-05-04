# ChamoxisBOT

Bot Discord pour rechercher des torrents via **Jackett (indexer C411)**, ajouter les téléchargements dans qBittorrent, puis importer automatiquement les médias vers une arborescence Plex (films/séries).

## Fonctionnalités

- Recherche de torrents via flux Torznab Jackett.
- Compatibilité **C411** par défaut (`JACKETT_INDEXER=c411`).
- Ajout de téléchargement via magnet ou upload de fichier `.torrent`.
- Suivi des torrents ajoutés par utilisateur Discord.
- Import automatique des médias terminés vers dossiers Films/Séries.
- Rafraîchissement optionnel des bibliothèques Plex.
- Gestion d’auth Jackett (anonyme, Basic Auth, cookie).
- Logs fichier + console.

## Prérequis

- Python 3.10+
- Un bot Discord (token)
- qBittorrent Web UI activée
- Jackett configuré avec un indexer **C411** opérationnel
- (Optionnel) Plex Media Server

## Installation

1. Cloner le dépôt.
2. Créer un environnement virtuel.
3. Installer les dépendances.
4. Créer le fichier `.env` à partir de `.env.example`.

Exemple (Linux/macOS):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install discord.py aiohttp python-dotenv
cp .env.example .env
```

## Configuration

Renseigner les variables dans `.env`.

### Variables obligatoires

- `DISCORD_TOKEN`
- `QBIT_URL`
- `QBIT_USER`
- `QBIT_PASS`
- `JACKETT_URL`
- `JACKETT_API_KEY`

### Variables Jackett / C411

- `JACKETT_INDEXER` : nom technique de l’indexer (défaut: `c411`).
- `JACKETT_RSS_URL` : optionnel, permet d’écraser l’URL générée automatiquement.
- `JACKETT_FORCE_UPLOAD` : `true/false` pour forcer l’upload `.torrent` au lieu du magnet.
- `JACKETT_USER` / `JACKETT_PASSWORD` : Basic Auth si Jackett est protégé.
- `JACKETT_COOKIE_NAME` / `JACKETT_COOKIE_VALUE` : cookie manuel si nécessaire.

### Variables Plex (optionnel)

- `PLEX_URL`
- `PLEX_TOKEN`
- `PLEX_MOVIES_SECTION_ID`
- `PLEX_SERIES_SECTION_ID`

### Logs

- `BOT_LOG_FILE` (défaut `bot.log`)
- `BOT_LOG_LEVEL` (défaut `INFO`)

## Migration YGG -> C411

Le support YGG a été retiré.

- L’indexer par défaut est désormais **C411**.
- Le flux RSS par défaut cible `/indexers/c411/...`.
- Si vous aviez une ancienne valeur YGG dans `.env`, remplacez-la par `c411`.

## Lancement

```bash
python bot.py
```

Au démarrage, le bot initialise la session qBittorrent, synchronise les commandes Discord et démarre la boucle de surveillance/import.

## Utilisation (Discord)

> Les commandes exactes dépendent des slash commands déclarées dans `bot.py` et synchronisées au serveur.

Workflow conseillé:

1. Rechercher un média depuis Discord.
2. Choisir un résultat torrent.
3. Lancer l’ajout dans qBittorrent (magnet ou `.torrent`).
4. Laisser le bot surveiller la fin du téléchargement.
5. Vérifier l’import dans dossiers Plex Films/Séries.

## Structure attendue des médias

Le bot classe les contenus via:

- catégorie torrent (`movies`/`series`) quand disponible,
- heuristiques de nommage (détection SxxExx, saison, etc.),
- préférences d’import attachées au torrent suivi.

## Dépannage

### Le bot ne trouve pas de résultats

- Vérifier que l’indexer C411 fonctionne dans Jackett.
- Tester l’URL Torznab manuellement dans un navigateur.
- Vérifier `JACKETT_API_KEY` et `JACKETT_INDEXER`.

### Erreurs 401/403/302 côté Jackett

- Activer `JACKETT_USER`/`JACKETT_PASSWORD` si Basic Auth.
- Renseigner `JACKETT_COOKIE_NAME`/`JACKETT_COOKIE_VALUE` si Jackett passe par cookie/session.

### qBittorrent refuse les ajouts

- Vérifier identifiants Web UI.
- Vérifier `QBIT_URL`.
- Vérifier que l’API qBittorrent est accessible depuis la machine du bot.

### Rien n’est importé vers Plex

- Vérifier les chemins locaux définis dans `bot.py` (`PLEX_ROOT`, `PLEX_MOVIES`, `PLEX_SERIES`).
- Vérifier les permissions d’écriture.

## Sécurité

- Ne jamais committer un `.env` réel.
- Régénérer le token Discord en cas de fuite.
- Restreindre les droits du bot Discord au strict nécessaire.

## Fichiers principaux

- `bot.py` : logique principale du bot.
- `.env.example` : exemple de configuration.
- `known_users.json` : base locale des utilisateurs connus (générée automatiquement).

