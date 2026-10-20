# Guide utilisateur ChamoxisBOT

ChamoxisBOT cherche des films et séries via Prowlarr, propose des torrents, puis suit les téléchargements qBittorrent et leur import vers Plex. L’IA aide à interpréter les demandes ; elle ne dispense jamais de confirmer un ajout.

## Les commandes utiles

| Commande | Usage |
|---|---|
| `/aide` | Guide privé avec menu de rubriques, accessible même sans autorisation |
| `/recherchetorrent query:…` | Recherche privée par titre ou phrase naturelle |
| `/telechargements` | Tes demandes, leurs états et leur progression disponible ; bouton Actualiser le suivi |
| `/ajoutertorrent link:…` ou `file:…` | Ajout manuel d’un magnet, d’une URL `.torrent` ou d’un fichier `.torrent` |
| `/ia question:…` | Poser une question en privé à l’IA locale |
| `/admin …` | Outils réservés aux administrateurs configurés |

Il n’est plus nécessaire de lancer `/info` ni d’enregistrer son compte avant de commencer. Les autorisations restent vérifiées.

## Demander un film ou une série

Exemples :

```text
/recherchetorrent query:Charlie et la Chocolaterie 2005 en 1080p
/recherchetorrent query:Les Anneaux de pouvoir
/recherchetorrent query:Grey's Anatomy S11 en 1080p multi
/recherchetorrent query:Andor S02E03 en 1080p
```

Sans `query`, la commande propose un bouton ouvrant le formulaire privé.

1. Vérifie **J’ai compris : rechercher…** et les critères affichés.
2. Si plusieurs œuvres correspondent, choisis la bonne fiche TMDb, avec son type et son année. Les listes ont 20 entrées par page.
3. Pour une série sans saison précisée, choisis la saison, puis **Saison entière** ou **Un épisode**. Une saison précisée sans épisode désigne un pack de cette saison. Le bot n’ajoute pas automatiquement tous les épisodes séparément si aucun pack n’est trouvé.
4. Consulte la date de première diffusion TMDb. **À venir** et **Date non annoncée** sont des informations, pas des blocages. Une saison dont la diffusion a commencé n’est pas nécessairement disponible en entier.
5. Choisis le torrent dans le menu. Consulte sa qualité, sa taille et ses seeds.
6. Clique **Confirmer l’ajout**. Choisir une œuvre, une saison, une qualité ou un torrent ne confirme pas un téléchargement.

Les titres de cette liste sont cliquables vers TMDb pour consulter le résumé et vérifier le découpage de la série. Cliquer sur un titre ouvre la fiche ; cela ne sélectionne pas l’œuvre dans le bot. Utilise ensuite le menu **Choisir une œuvre**. Le lien de l’œuvre choisie reste présent pendant les choix de saison/épisode, dans les propositions de torrents et après confirmation. Les recommandations de bibliothèque comportent également un lien lorsqu’un identifiant TMDb est connu ; aucun lien n’est inventé si l’œuvre n’est pas identifiée.

Le menu affiche la taille à côté des seeds et un indicateur : **🔥 au moins 20 seeds**, **👍 de 3 à 19**, **⚠️ de 0 à 2 ou nombre inconnu**. C’est un repère de disponibilité annoncée, pas une note de qualité vidéo ni une garantie de connexion. Ce repère ne change pas le classement des résultats.

**Corriger la recherche** ouvre ta demande initiale préremplie. **Annuler** ferme la recherche sans créer d’ajout. Les contrôles expirent après cinq minutes d’inactivité. Si un ajout n’a pas été confirmé par le module, **Vérifier l’ajout** reprend exactement la même demande pour éviter un doublon ; ne crée pas une nouvelle demande à l’aveugle.

## Parler au bot dans le salon

Dans les salons configurés, les utilisateurs autorisés peuvent écrire `bot …` ou `man …`, sans distinction de casse :

```text
man cherche Les Anneaux de pouvoir S01 en 1080p
bot ajoute Charlie et la Chocolaterie 2005
man tu peux me trouver Grey's Anatomy S11 ?
bot pourquoi mon dernier téléchargement est bloqué ?
man quelles séries de science-fiction sont disponibles sur Plex ?
bot salut, conseille-moi une série
```

Les verbes courants « cherche », « ajoute », « trouve », « télécharge », « mets-moi » ouvrent le même parcours. Une autre formulation peut être interprétée par l’IA, mais les formulations ci-dessus sont les plus fiables.

Pour une recherche, clique **Continuer en privé**. Ton message et le bouton restent publics ; les choix et résultats suivants sont visibles uniquement par toi. Les réponses de discussion et de consultation de bibliothèque dans un salon restent publiques. Pour discuter en privé, utilise `/ia`. Cette commande de discussion ne lance pas à elle seule une recherche ou un téléchargement.

Le mode texte nécessite les salons autorisés et Message Content Intent activé par l’administrateur. Les commandes `/` fonctionnent sans cet intent.

## Comment les torrents sont choisis

Le bot essaie les identifiants disponibles puis les titres français, anglais et original fournis par TMDb, avec des variantes à espaces et à points. Il arrête les variantes d’un tracker dès qu’une requête fournit, après pagination, des résultats respectant les critères, hors AV1 et avec les seeds souhaités. S’il ne trouve que de l’AV1 ou d’autres résultats de repli, il continue les variantes, avec une limite totale de douze requêtes par tracker. Les autres trackers configurés restent consultés. Les points et apostrophes des noms de releases sont pris en compte sans devoir saisir le nom exact du fichier.

Pour les titres doubles comme **Goksung (The Wailing)**, les deux noms sont également cherchés et reconnus séparément, sans devoir figurer ensemble dans la release. Les années ou annotations techniques entre parenthèses ne deviennent pas des titres alternatifs. La qualité demandée et les autres critères restent respectés.

- Le bot respecte la qualité précisée. Sans précision, il privilégie la qualité configurée, généralement 1080p, et peut considérer les autres qualités si elle est indisponible. Un changement d’une qualité explicitement demandée nécessite un choix de l’utilisateur.
- MULTI est privilégié, sauf langue demandée explicitement.
- Les seeds positifs sont privilégiés, puis le poids réduit parmi les candidats compatibles. Leur nombre ne garantit pas une connexion dans qBittorrent.
- Si seuls des résultats sans seed positif sont disponibles, un avertissement indique que le téléchargement peut rester longtemps en attente, sans garantie de finir.
- Les torrents AV1 ne sont proposés que si aucun autre codec ne respecte les critères de la recherche, avec un avertissement de compatibilité avant confirmation. Un torrent non AV1 compatible reste prioritaire même s’il est plus lourd ou n’annonce aucun seed. Le recours à AV1 ne change ni le titre, ni la saison/épisode, ni la langue ou la qualité explicitement demandée.
- Le titre, l’identité de l’œuvre et la saison/épisode sont vérifiés. L’année de la fiche d’une série est son année de lancement.

## Comprendre un problème

| Message ou symptôme | À vérifier |
|---|---|
| Mauvais titre dans « J’ai compris » | Corriger la recherche avec le titre exact, l’année et la saison |
| Aucun résultat transmis | Les indexers n’ont rien renvoyé ; vérifier titre et disponibilité |
| Résultats reçus, aucun retenu | Lire les exclusions : saison, qualité, langue, année, titre ou AV1 |
| Plusieurs compteurs d’exclusion | Un même torrent peut cumuler plusieurs motifs ; les nombres ne s’additionnent pas forcément |
| Recherche incomplète | Des indexers ont échoué ; cela ne prouve pas l’absence du contenu |
| Étape en échec | IA, TMDb ou module Plex indisponible ; utiliser Réessayer, puis transmettre l’étape à l’administrateur |
| Débit nul / aucun seed connecté | Attendre une source ; le téléchargement n’est pas garanti |
| Import long | Un déplacement entre disques peut prendre longtemps ; consulter le suivi avant de relancer |
| Le bot ignore un message texte | Vérifier préfixe bot/man, salon autorisé, accès utilisateur et Message Content Intent |

`/telechargements` montre uniquement tes demandes, même si tu es administrateur. Le bouton actualise les états et, lorsqu’elle est disponible, la progression qBittorrent des cinq demandes les plus récentes. Jusqu’à douze demandes sont affichées. Les demandes privées n’envoient pas de notifications de progression dans un salon public. Les administrateurs conservent une vue globale via leurs outils et le dashboard.

Une fois l’import terminé avec des fichiers importés, le bot publie une annonce de nouveau film ou de nouveaux épisodes dans le salon de nouveautés configuré. Elle ne contient ni demandeur, ni identifiant de tâche, ni chemins locaux. Un même suivi réutilise sa propre annonce ; la progression ne la remplace pas. Une actualisation Plex acceptée peut encore prendre un instant avant que le contenu soit visible. Si l’actualisation échoue ou n’a pas été confirmée, l’annonce le précise. Les simulations et imports sans fichier ne produisent pas d’annonce de nouveau contenu.

Dans `manager/.env`, renseigner `ALERT_CHANNEL_ID` avec le salon de nouveautés et inclure ce même identifiant dans `DISCORD_NOTIFICATION_CHANNEL_IDS`. Ces réglages sont également disponibles dans le dashboard. Le bot doit pouvoir voir le salon et y envoyer des messages. Pour les recherches privées, aucun autre salon n’est choisi automatiquement si cette configuration manque. Les logs indiquent alors pourquoi l’annonce est ignorée. Après mise à jour, redémarrer le manager et le module Plex ; les anciens événements déjà traités ne sont pas réannoncés.

## Administrer les utilisateurs

Seuls les identifiants présents dans `DISCORD_ADMIN_IDS` peuvent gérer les accès. Les permissions « Administrateur » du serveur Discord ne remplacent pas cette liste. Aucun droit d’administration ne peut être accordé avec les commandes d’ajout d’utilisateur.

```text
/admin utilisateurs ajouter membre:@Alice
/admin utilisateurs retirer membre:@Alice
/admin utilisateurs liste
/admin utilisateurs mode valeur:restreint
/admin utilisateurs mode valeur:ouvert
```

Les accès s’appliquent à cette installation du bot, aux messages `bot/man`, aux commandes utilisateur et aux boutons privés. Ils sont enregistrés dans la base SQLite du manager et prennent effet immédiatement, sans redémarrage ni modification du `.env`.

Au départ, une liste `DISCORD_ALLOWED_USER_IDS` renseignée donne un accès restreint ; une liste vide conserve l’accès ouvert. Ajouter quelqu’un ne change pas ce mode. Utilise explicitement **mode restreint** pour ne garder que les utilisateurs autorisés et administrateurs. En mode ouvert, les utilisateurs retirés restent exclus. Les restrictions de salons texte continuent de s’appliquer.

Les identifiants du `.env` sont conservés ; un retrait explicite via Discord prévaut sur l’autorisation utilisateur du `.env`. Les administrateurs configurés gardent toujours leur accès. Retirer un utilisateur empêche ses nouvelles actions et confirmations mais n’annule pas les téléchargements déjà confirmés. `/aide` reste accessible à tous.

Si aucun administrateur n’est configuré, renseigner localement `DISCORD_ADMIN_IDS` dans `manager/.env`, puis redémarrer le manager. Ne jamais publier de token dans Discord.

## Autres outils d’administration

- `/admin torrents` : vue privée des derniers torrents de tous les utilisateurs.
- `/admin services` : état des services supervisés.
- `/admin relancer service:…` : demander le redémarrage d’un service configuré comme géré.
- `/admin annuler_suivis` : demander une confirmation avant d’annuler les suivis actifs présents à cet instant. Les fichiers et torrents qBittorrent sont conservés ; les nouvelles demandes ne sont pas concernées. Cette action ne met pas les téléchargements en pause.

## Correspondance avec les anciennes commandes

| Ancienne commande | Nouvelle commande |
|---|---|
| `/info` | `/aide` |
| `/plex demande`, `/plex recherchetorrent`, ancien `/recherchetorrent` | `/recherchetorrent` avec le parcours privé moderne |
| `/plex operations` | `/telechargements` |
| `/addtorrent`, `/plex addtorrent` | `/ajoutertorrent` |
| `/status`, `/plex status` | `/admin torrents` ; `/telechargements` pour tes demandes |
| `/cleartorrents`, `/plex cleartorrents` | `/admin annuler_suivis` avec confirmation |
| `/server status`, `/server restart` | `/admin services`, `/admin relancer` |
| `/ai chat` | `/ia` |
| `/rssfeed`, `/plex rssfeed` | Retirées du menu ; utiliser la recherche ou l’ajout manuel |

Après mise à jour, redémarrer le manager pour synchroniser les commandes Discord globales et celles du serveur configuré. Discord peut conserver temporairement les anciennes suggestions : recharger l’application si nécessaire. Aucun nouveau champ `.env` n’est obligatoire ; conserver le dossier de données du manager pour conserver les autorisations.
