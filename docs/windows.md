# Installation Windows

## Préparer Python et les configurations

Installer Python **3.12** avec le lanceur `py`, Git et Ollama depuis leurs sources
officielles. Plex, Prowlarr et qBittorrent restent des applications externes déjà
configurées : ces scripts ne les installent pas et ne changent pas leurs paramètres.

Depuis PowerShell, à la racine du dépôt :

```powershell
./scripts/setup-windows.ps1
```

Le script crée `.venv-manager` et `.venv-plex`, installe leurs dépendances séparées,
copie les exemples **uniquement si les fichiers locaux sont absents**, puis lance
les tests. Il ne contourne pas la politique d’exécution PowerShell de la machine.

Éditer localement `manager/.env`, `modules/plex/.env` et `config/services.json`.
Ces fichiers et les données SQLite sont ignorés par Git. Ne pas partager les
secrets dans Discord, les tickets ou le chat de développement.

Générer trois clés aléatoires distinctes avec un gestionnaire de mots de passe :

| Clé | Où la renseigner |
| --- | --- |
| `MANAGER_API_KEY` | Les deux applications, pour les événements et l’API normale |
| `MANAGER_ADMIN_API_KEY` | Gestionnaire uniquement, pour les relances |
| `PLEX_MODULE_API_KEY` | Les deux applications, pour l’API du module Plex |

Le gestionnaire a besoin du token Discord, de `DISCORD_GUILD_ID`, des identifiants
d’administrateurs et des salons autorisés dans `DISCORD_NOTIFICATION_CHANNEL_IDS`.
Les administrateurs sont refusés par défaut si leur liste est vide. Une liste
`DISCORD_ALLOWED_USER_IDS` vide permet l’usage normal par les utilisateurs du bot.
Les slash commands fonctionnent sans Message Content Intent. Le mode
conversation ci-dessous nécessite son activation explicite.

## Conversations dans un salon

Dans `manager/.env`, renseigner les salons autorisés :

```dotenv
DISCORD_CONVERSATION_CHANNEL_IDS=123456789012345678
```

Ajouter ces salons à `DISCORD_NOTIFICATION_CHANNEL_IDS` pour recevoir le suivi.
Dans Discord Developer Portal → application → **Bot** → **Privileged Gateway
Intents**, activer **Message Content Intent**, puis redémarrer le gestionnaire.
Le bot demande cet intent uniquement si des salons conversationnels sont configurés.
Il doit pouvoir voir le salon et y envoyer des messages.

Dans le salon choisi :

```text
bot cherche Charlie et la Chocolaterie de 2005 en français en 1080p
bot, tu peux me trouver Grey’s Anatomy S11 ?
bot pourquoi mon dernier téléchargement semble bloqué ?
bot quelle différence entre 720p et 1080p ?
```

Le bot pose éventuellement une question, puis recherche le titre seul et propose
le résultat le mieux classé. Répondre `oui` pour ajouter ce résultat, `non` pour
annuler, ou un numéro pour choisir une autre proposition puis la confirmer.
Chaque utilisateur possède sa conversation dans chaque salon. La confirmation
expire après cinq minutes et une réponse adressée à un autre message ne confirme
pas la proposition. Les autres messages ne déclenchent pas de téléchargement.
Le mode test reste applicable. Tous les messages commençant par `bot` (avec ou sans
virgule) sont interprétés : recherche, état des téléchargements, état des services
(administrateurs uniquement), ou discussion. Les commandes de suppression, pause
et redémarrage ne sont pas exécutées par ce mode. Les commandes slash restent disponibles.
Une précision comme « plutôt en français » peut affiner une proposition en attente;
la nouvelle proposition demande une nouvelle confirmation. Pour discuter ou poser
une nouvelle question, commencer le message par `bot`.

Le bot conserve les dix derniers messages de chaque échange adressé au bot,
pendant trente minutes d'inactivité, séparément par utilisateur et salon. Il ne
récupère pas l'historique du salon. Ce contexte est envoyé au modèle configuré
(distant si disponible, sinon local). Les états expirés sont ignorés.
Les questions sur les téléchargements consultent les cinq dernières demandes créées
par cet utilisateur via le bot, avec l'état qBittorrent actuel des téléchargements
en cours. Les torrents ajoutés manuellement ne sont pas attribués à un utilisateur.
Le diagnostic distingue une observation actuelle d'une cause certaine; il ne
relance ni ne modifie le téléchargement. Si le modèle ne peut formuler la réponse
après la consultation, les observations disponibles sont affichées directement.

Le module reçoit les identifiants Prowlarr/qBittorrent/Plex. Les chemins
`PLEX_MOVIES_PATHS` et `PLEX_SERIES_PATHS` doivent être des dossiers existants
accessibles au compte Windows qui lance le module et cohérents avec les chemins
vus par qBittorrent. Plusieurs racines sont séparées par `|`.

L’exemple conserve **`DISABLE_TORRENT_DOWNLOAD=true`** pour les premiers essais.
Une confirmation crée alors une tâche `simulated` sans appel d’ajout ni import.
Passer explicitement à `false` seulement après validation de la configuration.

## Installer les modèles

Sur le serveur RTX 2060 :

```powershell
ollama pull qwen3:4b
ollama list
```

Sur le PC RTX 4080 Super :

```powershell
ollama pull qwen3:14b
ollama list
```

Vérifier la quantification et la taille effectivement téléchargées avec
`ollama show qwen3:4b` / `ollama show qwen3:14b`. Les variantes et leur occupation
mémoire dépendent de la version du catalogue Ollama. Les performances GPU n’ont
pas été mesurées dans le cloud. Le contexte est limité à 4096 tokens, la sortie
à 512 tokens et une seule génération à la fois par instance configurée. Réduire
`OLLAMA_CONTEXT` à 2048 si Plex et le petit modèle se disputent les 6 Go de VRAM.

Pour le PC secondaire, renseigner `OLLAMA_REMOTE_URL` et adapter son service dans
`services.json`. Ollama distant doit écouter sur l’interface LAN explicitement
choisie avec `OLLAMA_HOST`. Redémarrer Ollama après modification et autoriser
le port 11434 **uniquement depuis l’adresse du serveur Plex** dans le pare-feu.
Ne pas exposer Ollama sur Internet. Son API native n’apporte pas ici
d’authentification supplémentaire ; utiliser un tunnel ou un proxy authentifié
si le réseau n’est pas privé et maîtrisé.

Le modèle distant est essayé en premier. L’API `/api/tags` doit contenir le nom
exact du modèle ; une API répondante sans ce modèle n’est pas considérée prête.
Une erreur, un timeout ou un JSON invalide déclenche le repli local. Si les deux
échouent, le chat renvoie 503 et la recherche Discord classique reste disponible.
Une demande de film avec titre et année explicites peut aussi être interprétée
par le parseur limité de secours, toujours avec sélection et confirmation.

## Lancer et vérifier

Dans deux terminaux PowerShell :

```powershell
./scripts/start-plex.ps1
./scripts/start-manager.ps1
```

Les API écoutent par défaut sur `127.0.0.1:8761` et `127.0.0.1:8760` et exigent
`X-API-Key`. Les fichiers de logs tournants sont dans `data/plex` et `data/manager`.
Les connexions HTTP du gestionnaire et du module sont locales : leurs clés API
ne doivent pas transiter en HTTP sur un réseau non fiable.

Vérifier successivement :

1. Les réponses authentifiées `/health` des deux API et `/services` du gestionnaire.
2. La connexion Discord (`discord: ready` dans `/health`) et `/info`, puis `/status`.
3. Une recherche classique avec ses menus et la confirmation en mode test.
4. `/plex demande` et une interprétation correcte du titre, de l’année et des filtres.
5. `/plex operations` avec une tâche `simulated` et une notification dans un salon autorisé.
6. Le repli local lorsque le PC secondaire est éteint.

Les identifiants de salon et les permissions Discord doivent permettre au bot
d’envoyer, consulter et modifier les messages. Le bot met à jour un message
persistant par tâche, plutôt que de conserver une interaction expirée.

Arrêter manuellement avec Ctrl+C. Le module continue ses tâches si seul le
gestionnaire est arrêté. Ne pas exécuter `python bot.py` en parallèle.

## Supervision et démarrage automatique

`config/services.example.json` commence en **surveillance uniquement**. Sonarr,
Radarr et Ollama distant sont désactivés jusqu’à leur configuration. Les sondes
HTTP des applications externes indiquent leur disponibilité, pas la réussite
d’un téléchargement ni une authentification métier complète.

Pour gérer un processus Python, remplacer son entrée par exemple par :

```json
{
  "id": "plex-module",
  "name": "Chamoxis Plex module",
  "type": "python",
  "command": [".venv-plex/Scripts/python.exe", "-m", "modules.plex.main"],
  "desired": "running",
  "restart": true,
  "external_supervisor": false,
  "health_url": "http://127.0.0.1:8761/health",
  "health_key_env": "PLEX_MODULE_API_KEY",
  "startup_delay": 30,
  "interval": 15,
  "max_attempts": 3,
  "backoff": 10
}
```

Dans ce cas fournir `PLEX_ENV_FILE=modules/plex/.env` au processus parent, ou
placer la configuration du module dans l’environnement du compte de lancement.
Ne pas conserver une tâche planifiée de relance du même module. Pour un service
Windows, utiliser `type: windows_service` et son nom SCM exact dans
`windows_name`. Les contrôles SCM peuvent nécessiter des droits spécifiques.
Pour une application GUI, respecter sa session utilisateur ; ne pas supposer
qu’elle fonctionne sous SYSTEM. Les chemins et commandes viennent uniquement
du fichier administrateur, jamais d’une réponse IA ou d’un argument Discord.

Le gestionnaire attend les délais de démarrage et les dépendances déclarées,
limite les relances avec temporisation exponentielle, et conserve leur compteur
dans SQLite. Une santé stable pendant au moins 60 secondes réinitialise ce
compteur. `desired: stopped` désactive la surveillance et les relances ; cette
valeur n’arrête pas de force un programme déjà lancé.

Pour confier les deux applications au Planificateur de tâches Windows, garder
le module en surveillance uniquement et lancer explicitement :

```powershell
./scripts/register-startup.ps1
```

Par défaut, les tâches démarrent à l’ouverture de session et disposent de trois
tentatives de récupération à une minute d’intervalle. L’option `-AtStartup`
utilise SYSTEM et nécessite des droits administrateur ; vérifier auparavant
l’accès aux fichiers, aux bibliothèques et au réseau sans session interactive.
Le script refuse de remplacer une tâche existante. Les verrous locaux et la
politique `IgnoreNew` empêchent une seconde instance pour les mêmes dossiers
de données. Ne pas changer les dossiers de données pour contourner ces verrous.

Les scripts Windows sont fournis, mais leur exécution sur la machine cible et
les logiciels GUI n’ont pas été validés dans l’environnement Linux.
