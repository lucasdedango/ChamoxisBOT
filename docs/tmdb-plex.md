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
- `bot cherche Dune` demande quelle adaptation choisir lorsque TMDb trouve plusieurs œuvres. Répondre avec le numéro, puis confirmer le torrent séparément avec « oui ».
- `man trouve Below saison 1 de 2026 en 1080p` utilise les titres français/originaux et les identifiants de l’œuvre choisie.
- `/plex demande` propose également un choix d’œuvre, puis la recherche et une confirmation d’ajout.
- Le dashboard affiche les fiches, résumés, affiches et liens TMDb/IMDb. L’onglet Bibliothèque Plex filtre les contenus présents par type, titre et genre.

Les recommandations sont classées selon la note renseignée par Plex. Le bot n’invente pas une disponibilité à partir d’une fiche TMDb et ne connaît pas tes goûts personnels ni ton historique de visionnage. Les échanges généraux utilisent toujours Ollama.

## Recherche et limites

Les indexers sont interrogés largement par titre, sans concaténer qualité/langue/année. Le bot vérifie leurs capacités Torznab avant d’utiliser IMDb/TMDb. Il essaie au plus une recherche par identifiant et deux recherches par titre par indexer, puis retire les doublons. Un identifiant refusé n’empêche pas les recherches par titre.

Chaque recherche récupère jusqu’à 250 résultats par indexer, par pages de 100, 100 et 50 lorsque la pagination est prise en charge. Aucun réglage .env supplémentaire n’est nécessaire.

Sans qualité explicite, le 1080p (ou SEARCH_DEFAULT_QUALITY) est préféré et les autres qualités restent proposées en alternatives. Même sans cette ligne .env, la préférence est 1080p. Une qualité explicitement demandée est filtrée strictement; en l’absence de résultat, les autres qualités nécessitent un choix séparé.

La sélection conserve la qualité explicitement demandée et la saison/épisode demandés, exclut AV1, préfère MULTI, puis la petite taille parmi les résultats disponibles. Les torrents à seeds positifs passent devant ceux à zéro/inconnus. Si aucun candidat n’a de seeds, le bot propose quand même un torrent en prévenant que le téléchargement peut prendre beaucoup de temps, sans garantie d’aboutir. L’ajout exige toujours une confirmation.

Les métadonnées TMDb sont mises en cache 24 heures; l’inventaire Plex deux minutes. Le dashboard peut forcer l’actualisation. Chaque section est limitée à 2 000 œuvres par consultation; un avertissement signale l’inventaire partiel. Les filtres de genre utilisent les données de Plex : un genre absent peut empêcher une œuvre d’apparaître. Une erreur Plex est signalée comme indisponibilité, pas comme absence de films.

TMDb nécessite Internet et un jeton valide; Plex doit être accessible depuis le module. Un problème TMDb conserve la recherche par titre et le bot le signale. Les tests automatisés utilisent des services simulés; le fonctionnement avec les comptes et services locaux doit être vérifié après configuration.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## Affiner une proposition

Sur Discord, répondre **autre recherche**, **affine la recherche** ou **ce n’est pas le bon** dans les cinq minutes réutilise l’œuvre identifiée et les préférences. Le bot essaie le titre avec saison/épisode, avec année, puis année et saison, ainsi que TMDb si l’indexer annonce ce support. Les doublons sont retirés. Cette recherche peut être plus lente et dépend toujours des résultats exposés par Prowlarr. La nouvelle proposition nécessite sa propre confirmation; une réponse à une ancienne proposition ne la confirme pas. Un ajout dont la confirmation est incertaine bloque cette relance.

Les logs `Indexer page` indiquent offset, nombre demandé et nombre reçu pour diagnostiquer la pagination.
