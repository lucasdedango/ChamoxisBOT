# Market : bot Discord autonome Leboncoin et Vinted

Ce module reprend le bot Market v2.7 fourni, avec ses parseurs Vinted/Leboncoin,
ses intervalles aléatoires et la session Brave/Chromium externe de vérification
manuelle Leboncoin. Il dispose d’un **autre bot Discord** : il ne démarre ni
n’utilise le bot Plex, son IA ou ses téléchargements.

## Installation Windows

Depuis la racine du dépôt, avec Python 3.12 :

```powershell
py -3.12 -m venv .venv-market
.\.venv-market\Scripts\python.exe -m pip install -c constraints.txt -r modules/market/requirements.txt
if (-not (Test-Path modules/market/.env)) { Copy-Item modules/market/.env.example modules/market/.env }
```

La copie du modèle `.env` s’effectue une seule fois : conserver un fichier local
existant. Éditer ensuite `modules/market/.env` :

```dotenv
MARKET_DISCORD_TOKEN=TOKEN_DU_BOT_MARKET
MARKET_CHANNEL_ID=IDENTIFIANT_DU_SALON_MARKET
MARKET_GUILD_ID=IDENTIFIANT_DU_SERVEUR_DISCORD
MARKET_ADMIN_IDS=TON_IDENTIFIANT_DISCORD
MARKET_ALLOWED_USER_IDS=
MARKET_DATA_DIR=data/market
MARKET_HEALTH_PORT=8762
```

Créer/inviter une application Discord dédiée à Market avec les permissions de
voir le salon, y envoyer des messages et intégrer des liens, ainsi que les commandes
d’application. Message Content Intent n’est pas nécessaire. `MARKET_GUILD_ID`
est facultatif et permet une synchronisation directe dans le serveur configuré.
Tu peux réutiliser l’application Discord de l’ancien bot Market : il s’agit
simplement d’un bot distinct de celui de Plex. Après synchronisation, recharger
Discord si les anciennes commandes restent temporairement dans les suggestions.

Le token Market est distinct de `DISCORD_TOKEN` du manager Plex. Même si un token
Plex existe dans l’environnement du PC, Market ne le récupère pas implicitement.
L’ancien format `DISCORD_TOKEN` et `DISCORD_CHANNEL_ID` reste accepté **uniquement
dans le fichier `.env` Market explicitement chargé**, pour reprendre l’ancien bot.
Préférer les nouveaux noms `MARKET_…` et éviter les doublons de clés.

Pour valider les fichiers sans connexion à Discord ou aux sites :

```powershell
$env:MARKET_ENV_FILE = 'modules/market/.env'
.\.venv-market\Scripts\python.exe -m modules.market.main --check
```

Puis double-cliquer sur **`start.bat`** à la racine pour lancer Plex, le manager
et Market dans trois fenêtres. Pour lancer uniquement Market, utiliser
**`scripts\start-market.bat`**, ou lancer :

```powershell
$env:MARKET_ENV_FILE = 'modules/market/.env'
.\.venv-market\Scripts\python.exe -m modules.market.main
```

Plex, le manager et Market peuvent fonctionner simultanément, chacun dans son
processus. Les dépendances de Market sont dans `.venv-market` et sa configuration
dans `modules/market/.env`. Ne pas lancer l’ancien
`market_bot.py` en parallèle après migration. Un verrou empêche deux instances
de ce module d’utiliser le même dossier de données.

## Reprendre les données de l’ancien bot

### Fenêtre de vérification et instance déjà active

Lorsqu’une page demande une intervention, Market tente de restaurer la fenêtre
Brave/Chrome/Edge et de sélectionner son onglet. Elle apparaît sur le PC et dans
la session Windows qui exécutent Market : une tâche sans session interactive
ne peut pas afficher une fenêtre sur ton bureau. Une page sans annonce lisible
est signalée séparément ; elle ne prouve pas la présence d’un CAPTCHA.

Pour arrêter Market, utiliser Ctrl+C dans sa fenêtre et attendre la fin du
processus. Si le redémarrage signale une instance active, vérifier le PID indiqué
dans le Gestionnaire des tâches (onglet Détails, colonne Ligne de commande).
Terminer uniquement le Python dont la commande contient `modules.market.main`,
puis relancer. Ne pas arrêter les Python de Plex ou du manager. La présence du
fichier `market.lock` seule est normale : le supprimer ne libère pas un verrou
détenu par un processus. Le PID enregistré peut être ancien ; toujours vérifier
la commande avant de terminer un processus.

## Migration des données

Arrêter l’ancien processus, conserver son dossier comme sauvegarde, puis copier
ses JSON avec cette commande (le dossier Market de destination ne doit pas encore
contenir `config.json`, `searches.json` ou `state.json`) :

```powershell
.\.venv-market\Scripts\python.exe -m modules.market.main --import-legacy 'C:\AncienMarketBot'
```

Cette commande ne démarre pas le bot et ne remplace aucun fichier existant. Elle
copie seulement `config.json`, `searches.json` et `state.json`. Elle ne copie pas
le token ou les cookies/profils navigateur. Reporter le token localement dans
`modules/market/.env` et renseigner les administrateurs Market.

Au premier chargement, les recherches sans propriétaire sont attribuées au plus
petit identifiant présent dans `MARKET_ADMIN_IDS`. L’état des annonces déjà vues
est conservé ; l’ancien état Vinted unique est rattaché à la première recherche
Vinted. Les originaux restent intacts. Les utilisateurs ajoutés par la suite
créent leurs propres recherches ; les administrateurs peuvent gérer les anciennes.

Pour conserver les sessions de navigateur, reprendre manuellement le dossier de
profil et/ou ajuster `leboncoin_browser_profile_dir` dans le nouveau `config.json`.
Un chemin relatif est résolu dans `data/market`, un chemin absolu permet de
reprendre un profil dédié existant. Ne pas partager un profil avec un ancien bot
encore en fonctionnement.

## Administrer les accès

Les autorisations sont indépendantes de Plex. Au départ, seuls les administrateurs
Market et les identifiants éventuels de `MARKET_ALLOWED_USER_IDS` ont accès.
Une liste utilisateur vide **n’ouvre pas l’accès à tout le serveur**.

Dans le salon Market :

```text
/admin utilisateurs ajouter membre:@Alice
/admin utilisateurs retirer membre:@Alice
/admin utilisateurs liste
```

Ces commandes sont réservées aux identifiants de `MARKET_ADMIN_IDS`. Les droits
« Administrateur » Discord et les administrateurs Plex ne donnent pas d’accès
Market automatiquement. Ajouter un utilisateur ne lui donne pas de droits admin.
Les ajouts/retraits sont persistés dans `data/market/access.sqlite3` sans écrire
dans le `.env`. Un retrait prévaut sur l’autorisation utilisateur du `.env`.
Les administrateurs configurés gardent leur accès.

Retirer un utilisateur suspend ses recherches, y compris les publications
retardées d’une session navigateur. Les recherches et leur historique sont
conservés. Réautoriser cet utilisateur reprend les recherches qui n’étaient pas
en pause. Une lecture déjà engagée peut se terminer ; aucune nouvelle annonce
de cette recherche n’est publiée après le contrôle d’autorisation.

## Ajouter et gérer une surveillance

1. Ouvrir Vinted ou Leboncoin et configurer les mots-clés, prix, localisation,
   catégorie et autres filtres directement sur le site.
2. Copier l’URL complète de la page de recherche.
3. Dans le salon Market, lancer par exemple :

```text
/recherche ajouter url:https://www.vinted.fr/catalog?search_text=telephone nom:Téléphone minimum:10 maximum:20
/recherche ajouter url:https://www.leboncoin.fr/recherche?text=velo nom:Vélo minimum:12 maximum:18
```

L’utilisateur devient propriétaire de la recherche. Il peut gérer uniquement
ses recherches ; un administrateur Market peut toutes les gérer. Les réponses
de commandes et listes sont privées, mais **les nouvelles annonces sont publiées
dans le salon Market partagé**. Le nom de la recherche est visible dans ces annonces.

Le premier passage mémorise les annonces existantes sans les publier. Les annonces
nouvelles des passages suivants sont envoyées avec titre, prix, lien et image
lorsqu’elle est disponible. Les annonces déjà mémorisées ne sont pas renvoyées à
chaque contrôle. Les mentions automatiques sont désactivées.

| Commande | Fonction |
|---|---|
| `/aide` | Guide rapide Market |
| `/etat` | État de ses surveillances, ou de toutes pour un administrateur |
| `/recherche liste` | Liste paginée de ses recherches et identifiants |
| `/recherche details identifiant:…` | URL et état d’une recherche |
| `/recherche intervalle identifiant:… minimum:10 maximum:20` | Changer l’intervalle aléatoire |
| `/recherche pause identifiant:…` | Suspendre une surveillance |
| `/recherche reprendre identifiant:…` | Réactiver une surveillance autorisée |
| `/recherche verifier identifiant:…` | Contrôle manuel supplémentaire avec délai minimum |
| `/recherche supprimer identifiant:…` | Suppression avec bouton de confirmation |

Les champs d’identifiant proposent les recherches accessibles en autocomplétion.
Les anciennes commandes `search_add`, `search_list`, `search_interval`,
`search_toggle`, `search_remove` et `search_check` sont remplacées par ce groupe
`/recherche`. L’ancienne `lbc_verify` devient `/admin leboncoin_verifier`.
Supprimer une recherche ne supprime pas les messages déjà envoyés. Recréer la même
surveillance produit un nouvel identifiant et un nouveau passage d’initialisation.

Par défaut : dix recherches maximum par utilisateur, intervalle minimum cinq
minutes, et au moins une minute entre deux contrôles manuels d’une même recherche.
Les contrôles normaux sont sérialisés ; les sessions Leboncoin persistantes ont
leur propre boucle. Une attente après blocage n’est pas annulée par une demande
manuelle, une reprise ou un changement d’intervalle.

## Leboncoin et navigateur

La version jointe utilisait un Brave/Chrome/Edge installé sur le PC, auquel
Playwright se connecte localement. Ce fonctionnement est conservé. Playwright
est installé avec les dépendances ; il n’est pas nécessaire de télécharger son
Chromium pour le mode navigateur externe par défaut. Une installation locale de
Brave/Chrome/Edge reste nécessaire, dans une session Windows avec bureau visible.

En cas de vérification demandée par Leboncoin, ouvrir la fenêtre laissée visible
sur le PC du bot et effectuer la vérification soi-même. Les autres surveillances
peuvent continuer. Un administrateur peut ouvrir/réutiliser cette session avec :

```text
/admin leboncoin_verifier identifiant:leboncoin-…
```

Cette commande ne résout pas de CAPTCHA automatiquement. Une même recherche ne
démarre pas une seconde session si la première est déjà active. Les recherches
distinctes utilisent des onglets distincts. Mettre en pause, supprimer ou retirer
l’accès du propriétaire arrête la boucle de surveillance correspondante ; le
navigateur dédié peut rester ouvert.

## Fichiers et réglages

`data/market/` contient les JSON de recherches/état, la base des autorisations,
`market.log` et les profils navigateur éventuels. Le dossier est local et ignoré
par Git. Le token reste dans `modules/market/.env`, également ignoré.

Un `config.json` est facultatif ; les valeurs absentes prennent leurs défauts.
Exemple de réglages sans secrets, à créer localement dans `data/market/config.json` :

```json
{
  "request_timeout_seconds": 25,
  "between_searches_seconds": 4,
  "max_searches_per_user": 10,
  "minimum_interval_minutes": 5,
  "manual_check_cooldown_seconds": 60,
  "leboncoin_external_browser_only": true,
  "leboncoin_browser_executable_path": "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
  "leboncoin_browser_profile_dir": "browser_profile_lbc_external",
  "leboncoin_cdp_host": "127.0.0.1",
  "leboncoin_cdp_port": 9222
}
```

Le port de contrôle du navigateur reste sur `127.0.0.1`. Les URL ajoutées par les
utilisateurs sont limitées aux recherches HTTPS des domaines Vinted.fr et
Leboncoin.fr explicitement pris en charge. Les redirections HTTP vers d’autres
domaines sont refusées. Un JSON illisible bloque le chargement et reste conservé.

## Intégration au gestionnaire ChamoxisBOT

Le module n’a besoin d’aucune clé API du manager/Plex. Sa santé locale est
consultable sur `http://127.0.0.1:8762/health` : HTTP 200 lorsque le client Discord
est prêt, 503 sinon. Cette réponse n’expose ni token, ni URL de recherche, ni
annonce et ne certifie pas que les sites tiers sont accessibles.

`config/services.example.json` inclut `market-module`, désactivé par défaut.
Pour l’afficher dans le dashboard existant, ajouter cette entrée à ton
`config/services.json` local avec `desired: "monitor"`. Lancer Market séparément.
Les fichiers de configuration existants ne sont pas remplacés par cette mise à jour.

## Vérifications et limites

Les tests couvrent les commandes, les propriétaires, les accès persistants, les
retraits, les premiers passages silencieux, les annonces suivantes, la migration,
les URL/redirections, la séparation des onglets, les parseurs sur pages de test,
la santé HTTP locale et le verrou d’instance. Aucun test ne publie sur un vrai
Discord ni ne lance de navigateur avec les cookies d’un utilisateur.

Les essais réels sur Vinted/Leboncoin dépendent de leur HTML, des restrictions
réseau et des vérifications humaines. Ils restent à effectuer sur le PC du bot.
Le mode Brave externe est conçu pour Windows ; les parseurs, permissions et
commandes sont également testables sous Linux sans interface graphique.

## Améliorations repérées, non implémentées

Comme demandé, ces points sont documentés sans étendre automatiquement le périmètre :

1. **Livraison durable des annonces.** Le code v2.7 mémorise les nouveaux IDs avant
   l’envoi Discord ; une panne d’envoi peut donc faire manquer une annonce.
   Une file persistante avec accusé d’envoi améliorerait la fiabilité.
2. **Limite globale de charge et de sessions navigateur.** Les quotas sont par
   utilisateur ; beaucoup d’utilisateurs peuvent encore multiplier les onglets.
   Une file globale et un plafond de sessions seraient utiles.
3. **Mutualiser les URL identiques entre utilisateurs.** Actuellement chacun a
   son suivi. Un seul contrôle partagé pourrait alimenter plusieurs abonnés.
4. **Déduplication déterministe à long terme.** L’ancien cache des IDs utilise un
   ensemble tronqué ; une conservation chronologique ou SQLite serait plus fiable
   lorsque les 8 000 IDs mémorisés sont dépassés.
5. **Persister les prochaines échéances et les backoffs.** Les états sont conservés,
   mais les échéances en mémoire sont recalculées après redémarrage.
6. **Parseurs et diagnostics.** Ajouter des pages réelles anonymisées en fixtures,
   distinguer plus finement une recherche vide d’un changement de HTML, et vérifier
   strictement les liens d’annonces extraits.
7. **Confort utilisateur.** Formulaire d’ajout, modification de l’URL/du nom,
   alertes en messages privés ou salons dédiés, transfert de propriétaire,
   gestion complète de Market depuis le dashboard. Ces fonctions ne sont pas
   ajoutées dans cette version.
