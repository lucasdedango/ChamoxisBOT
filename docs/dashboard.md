# Chamoxis Dashboard local

Le dashboard reprend le style du dashboard AzerothCore : fond sombre, cartes,
onglets et actions explicites. Il utilise les API du gestionnaire et du module
Plex et peut rester ouvert quand ces deux services sont arrêtés.

## Installation et lancement

Dans le clone `C:\AIbot`, récupérer la branche contenant le dashboard :

```powershell
cd C:\AIbot
git pull --ff-only origin test/manager-plex-ollama
```

Double-cliquer sur `dashboard.bat`. Il utilise `.venv-manager` et ses dépendances
existantes; aucun nouveau paquet n'est nécessaire. Le navigateur s'ouvre sur
`http://127.0.0.1:8765/` avec une connexion automatique à usage unique valable
120 secondes. Ne pas diffuser le lien contenant ce jeton. Le jeton est supprimé
de l'adresse dès l'ouverture. Pour une ouverture manuelle, utiliser la clé locale
`MANAGER_ADMIN_API_KEY` de `manager/.env`. La session dure huit heures, reste
uniquement dans un cookie HttpOnly, et le serveur accepte uniquement les origines
locales pour les actions. Il n'écoute pas sur le réseau.

Les réglages sont lus depuis `manager/.env` et `modules/plex/.env` à la racine.
Lancer les applications avec `start.bat` pour voir les états actifs; le dashboard
ne remplace pas le lanceur des services. Pour arrêter, Ctrl+C dans leurs fenêtres.
Si le navigateur s'ouvre trop tôt, actualiser la page. Si le port 8765 est occupé,
fermer l'autre instance. Le verrou `data/dashboard/dashboard.lock` évite le double
lancement du dashboard.

## Fonctions

- **Accueil** : connexion Discord, services supervisés, demandes actives, CPU/RAM,
  espace libre des dossiers films et séries, liens vers les applications locales.
- **Téléchargements** : état des demandes, utilisateur Discord, progression, débit;
  une fiche consulte aussi l'état actuel du torrent pendant le téléchargement.
- **Recherche** : demande naturelle, extraction des critères, classement identique
  au bot, puis confirmation explicite. Aucun téléchargement ne part à la recherche.
  L'identifiant du premier administrateur configuré est utilisé par défaut; on peut
  renseigner son identifiant Discord avant la recherche. Les confirmations expirent
  après cinq minutes; un nouvel essai de la même confirmation réutilise son identifiant
  de demande. Le mode test du module Plex est toujours respecté.
- **IA** : présence du modèle configuré local/distant et chat général sans actions.
- **Configuration** : formulaire des paramètres pris en charge, secrets masqués,
  validation et sauvegarde dans `data/dashboard/config-backups`. Un secret laissé
  vide est conservé. Les clés partagées entre les deux applications doivent être
  modifiées dans les deux formulaires avec la même valeur. Un redémarrage applique
  les changements; le dashboard affiche les applications concernées.
- **Logs** : dernières lignes du gestionnaire/module, filtre niveau et identifiant,
  export d'un rapport JSON avec les secrets connus masqués. Les rapports contiennent
  encore titres, identifiants Discord et chemins locaux : les relire avant partage.
- **Services** : la relance apparaît seulement pour les services explicitement gérés
  (`external_supervisor=false`, type process/python/windows_service, desired running).
  Les applications externes seulement surveillées ne sont pas arrêtées par le dashboard.

Les services indisponibles sont signalés sans empêcher d'ouvrir les configurations
et les mises à jour. Les logs attendus sont `data/manager/manager.log` et
`data/plex/plex.log` (ou `bot.log` pour le module historique). Un dossier de données
personnalisé sous la racine est pris en compte. Les raccourcis Plex/qBittorrent/Prowlarr
utilisent leurs ports locaux habituels.

## Mise à jour et choix de branche

Source unique : `https://github.com/lucasdedango/ChamoxisBOT`.
Git doit être installé et le dossier doit être un clone Git. Le dashboard ne
fusionne aucune branche sur GitHub et ne pousse pas de commit.

1. Ouvrir **Mises à jour** puis **Charger les branches**.
2. Choisir une branche publiée et cliquer **Préparer l'aperçu**. Le dashboard
   récupère les objets Git dans un cache séparé, vérifie le manifeste et affiche
   le commit, les fichiers ajoutés/modifiés/supprimés et les dépendances concernées.
   Le code installé reste inchangé à cette étape.
3. Vérifier les changements et arrêter le gestionnaire et le module Plex avec
   Ctrl+C. Laisser la fenêtre du dashboard ouverte.
4. Cliquer **Sauvegarder et appliquer**, puis confirmer. Si les ports API sont
   encore ouverts, la mise à jour est refusée. Une archive du code actuel et des
   configurations locales est créée dans `data/dashboard/update-backups` avant
   de passer la branche Git locale au commit exact affiché.
5. Fermer et relancer `dashboard.bat`. Si les dépendances ont changé, les installer
   avec les deux environnements Python, puis lancer `start.bat` :

```powershell
.\.venv-manager\Scripts\python.exe -m pip install -c constraints.txt -r manager/requirements.txt
.\.venv-plex\Scripts\python.exe -m pip install -c constraints.txt -r modules/plex/requirements.txt
```

L'installation de paquets et le redémarrage ne sont jamais lancés automatiquement
par une mise à jour. Les données et configurations ignorées par Git restent en place.
Le dashboard refuse les fichiers privés, liens symboliques et sous-modules dans
la branche cible, les collisions avec des fichiers locaux non suivis, les fichiers
suivis modifiés, ainsi que l'écrasement de commits locaux absents de la branche cible.
Un aperçu expire après quinze minutes.

Le choix d'une autre branche suit le même processus et retire les fichiers suivis
absents de la version choisie. Les branches doivent contenir le dashboard modulaire
et un manifeste complet; une ancienne branche avec uniquement `bot.py` est refusée.
En particulier, `main` peut apparaître dans la liste avant de contenir cette nouvelle
architecture : sa présence ne garantit pas sa compatibilité.

Chaque archive contient `update.json` avec le commit et la branche précédents,
`code/` et `configuration/`. Garder ces sauvegardes privées : elles contiennent les
secrets locaux. Elles permettent de retrouver les fichiers antérieurs en cas de
problème. La mise à jour ne restaure pas automatiquement une base SQLite ni les
fichiers `.env`; ces fichiers n'ont pas été remplacés.

## Manifeste de mise à jour

`dashboard-update-manifest.json` liste tous les fichiers suivis de chaque version.
Après ajout/suppression d'un fichier source, le régénérer avant commit :

```powershell
.\.venv-manager\Scripts\python.exe scripts/update-dashboard-manifest.py
```

L'outil inclut les fichiers suivis et les nouveaux fichiers source autorisés, exclut
les données locales et valide les chemins. Le manifeste, la branche et le commit
exact sont contrôlés avant toute modification. Ce mécanisme distribue le code du
dépôt choisi; il ne constitue pas une vérification de sécurité des futures versions.
