# TMDb et bibliothèque Plex

TMDb est une API en ligne : aucun programme à installer, aucun modèle supplémentaire à télécharger. Le bot continue à fonctionner sans TMDb avec la recherche par titre.

## Configuration Windows

1. Créer un compte sur https://www.themoviedb.org, puis ouvrir les paramètres du compte → API. Demander un accès API et copier **API Read Access Token** (jeton Bearer, différent de la clé API courte).
2. Dans `manager/.env`, ajouter :

```dotenv
TMDB_ACCESS_TOKEN=ton_jeton_local
TMDB_LANGUAGE=fr-FR
```

Le dashboard permet aussi de renseigner ces champs dans Configuration → Gestionnaire. Le jeton reste masqué. Ne le publie pas sur GitHub et ne le colle pas dans Discord.

3. Pour consulter ce qui existe réellement sur Plex, vérifier dans `modules/plex/.env` :

```dotenv
PLEX_URL=http://127.0.0.1:32400
PLEX_TOKEN=ton_jeton_plex_local
PLEX_MOVIES_SECTION_ID=1
PLEX_SERIES_SECTION_ID=2
```

Les identifiants sont ceux des bibliothèques Plex, pas des dossiers. Laisser les identifiants de sections vides consulte toutes les sections films/séries. Un identifiant incorrect produit une erreur, sans annoncer une bibliothèque vide. Pour obtenir le token Plex, suivre https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/.

4. Arrêter les services et le dashboard, puis relancer `start.bat` et `dashboard.bat`. Les dépendances Python existantes suffisent.

## Utilisation

- `man qu’est-ce que je peux regarder comme série de SF sur le Plex ?` consulte les séries réellement présentes et leur genre Plex. SF et science-fiction sont équivalents.
- `bot cherche Dune` demande quelle adaptation choisir lorsque TMDb trouve plusieurs œuvres. Cliquer sur « Continuer en privé », choisir l’œuvre dans le menu, puis confirmer le torrent avec le bouton dédié.
- `man trouve Below saison 1 de 2026 en 1080p` utilise les titres français/originaux et les identifiants de l’œuvre choisie.
- `/recherchetorrent` propose également un choix d’œuvre, puis la recherche et une confirmation d’ajout.
- Le dashboard affiche les fiches, résumés, affiches et liens TMDb/IMDb. L’onglet Bibliothèque Plex filtre les contenus présents par type, titre et genre.

Les recommandations sont classées selon la note renseignée par Plex. Le bot n’invente pas une disponibilité à partir d’une fiche TMDb et ne connaît pas tes goûts personnels ni ton historique de visionnage. Les échanges généraux utilisent toujours Ollama.

## Recherche et limites

Les indexers sont interrogés largement par titre, sans concaténer qualité/langue/année. Le bot vérifie leurs capacités Torznab avant d’utiliser IMDb/TMDb. Chaque recherche utilise les identifiants IMDb et TMDb si disponibles et pris en charge, puis les variantes du titre français/original avec saison/épisode, année, année et saison, ainsi que les titres seuls. Les requêtes identiques et résultats en double sont retirés. Les filtres sont appliqués après récupération. Un identifiant refusé n’empêche pas les recherches par titre.

Chaque recherche récupère jusqu’à 250 résultats par indexer, par pages de 100, 100 et 50 lorsque la pagination est prise en charge. Aucun réglage .env supplémentaire n’est nécessaire.

Sans qualité explicite, le 1080p (ou SEARCH_DEFAULT_QUALITY) est préféré et les autres qualités restent proposées en alternatives. Même sans cette ligne .env, la préférence est 1080p. Une qualité explicitement demandée est filtrée strictement; en l’absence de résultat, les autres qualités nécessitent un choix séparé.

La sélection conserve la qualité explicitement demandée et la saison/épisode demandés, ne propose AV1 que si aucun autre codec compatible n’existe dans les résultats reçus (avec avertissement), préfère MULTI, puis la petite taille parmi les résultats disponibles. Les torrents à seeds positifs passent devant ceux à zéro/inconnus. Si aucun candidat n’a de seeds, le bot propose quand même un torrent en prévenant que le téléchargement peut prendre beaucoup de temps, sans garantie d’aboutir. L’ajout exige toujours une confirmation.

Les métadonnées TMDb sont mises en cache 24 heures; l’inventaire Plex deux minutes. Le dashboard peut forcer l’actualisation. Chaque section est limitée à 2 000 œuvres par consultation; un avertissement signale l’inventaire partiel. Les filtres de genre utilisent les données de Plex : un genre absent peut empêcher une œuvre d’apparaître. Une erreur Plex est signalée comme indisponibilité, pas comme absence de films.

TMDb nécessite Internet et un jeton valide; Plex doit être accessible depuis le module. Un problème TMDb conserve la recherche par titre et le bot le signale. Les tests automatisés utilisent des services simulés; le fonctionnement avec les comptes et services locaux doit être vérifié après configuration.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## Une seule recherche complète

La recherche complète est utilisée dès le départ sur Discord, avec `/recherchetorrent`, dans les commandes classiques et dans le dashboard. Il n’y a plus de passage préalable par une recherche simple. Les recherches larges par titre restent présentes pour retrouver les releases sans année; elles sont combinées aux identifiants et aux variantes ciblées.

Le bouton **Corriger la recherche** ouvre un formulaire privé pour ajuster l’œuvre ou les critères. Un ancien bouton ne peut pas confirmer une proposition remplacée. Un ajout dont la confirmation reste incertaine conserve sa protection : seul **Vérifier l’ajout** est proposé, avec le même identifiant de demande.

Cette stratégie peut être plus lente et reste dépendante de ce que Prowlarr et les indexers exposent. Les délais des clients Discord/dashboard ont été adaptés. Aucun téléchargement n’est confirmé par une recherche ou un choix de fiche.

## Ambiguïtés TMDb

Le catalogue interroge systématiquement **films et séries**. Le type deviné par l’IA sert seulement à ordonner les choix; il ne masque plus l’autre catégorie. Les titres plausibles sont comparés à la demande dans les titres français et originaux. Une correspondance exacte ne masque pas les titres plus longs correspondant à la même recherche.

Ainsi, **Harry Potter** présente aussi les films lorsque la série homonyme apparaît. **Dune** demande de choisir entre les œuvres correspondantes. Une année explicite réduit les choix si des fiches correspondent. Un titre précis avec son année peut être sélectionné automatiquement seulement s’il reste une unique correspondance exacte et si les deux recherches TMDb ont réussi.

Discord affiche les fiches par pages de **vingt**, avec un menu indiquant le type et l’année et des boutons **Suivant/Précédent**. `/recherchetorrent` ouvre directement ce parcours privé. Depuis « bot/man cherche… », un bouton public **Continuer en privé** ouvre la suite visible uniquement par l’auteur. Le dashboard affiche également le type film/série. Au plus quarante fiches issues des premières pages films/séries TMDb sont disponibles; un avertissement invite à préciser le titre au-delà. Les premières pages de recherche TMDb sont utilisées, ce qui ne garantit pas un inventaire exhaustif d’une très grande franchise.

Si une catégorie TMDb est indisponible, le bot ne sélectionne pas automatiquement l’unique résultat de l’autre catégorie. Si aucune fiche ne correspond à l’année demandée, il demande un choix avec un avertissement. Le choix de fiche mène ensuite aux torrents, puis à une confirmation d’ajout distincte.

Les logs `Indexer page` indiquent offset, nombre demandé et nombre reçu pour diagnostiquer la pagination.

### Vérifier que TMDb est actif dans le gestionnaire

Après un changement de dossier, copier aussi le jeton TMDb dans le fichier `.env` effectivement chargé par le gestionnaire (normalement `manager/.env` via `start.bat`), puis redémarrer le gestionnaire. Le dashboard peut voir un jeton enregistré dans un fichier sans que le processus déjà démarré l’ait chargé. Si le jeton manque dans le processus, les propositions de torrents et les logs de démarrage indiquent désormais explicitement « Identification TMDb inactive ». La route authentifiée `/health` du gestionnaire expose aussi `tmdb_configured`, sans dévoiler le jeton. Sans TMDb, la recherche utilise seulement le titre et ne peut pas présenter le choix des fiches films/séries en ligne.
