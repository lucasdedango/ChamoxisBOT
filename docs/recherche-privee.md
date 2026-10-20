# Recherche privée et nettoyage des logs

## Discord

- `man cherche Harry Potter` ou une demande naturelle de recherche affiche un bouton **Continuer en privé**. Le message de départ de l’utilisateur et ce bouton restent visibles dans le salon; les résultats suivants sont une réponse éphémère visible uniquement par l’auteur. Les messages normaux Discord ne peuvent pas recevoir directement une réponse éphémère sans clic.
- `/plex demande texte:Harry Potter` ouvre directement la recherche privée.
- Les œuvres sont affichées **20 par page**, avec type/année dans le menu et boutons de navigation. Les premières pages films et séries TMDb peuvent fournir jusqu’à 40 fiches. Les torrents ont également un menu paginé par 20.
- Choisir une œuvre lance la recherche complète. Choisir un torrent change seulement la sélection. Seul le bouton **Confirmer l’ajout** crée une demande.
- Un autre choix de qualité relance la recherche sans télécharger. **Préciser la demande** ouvre un formulaire privé pour corriger le titre ou les critères. **Annuler** ne crée aucun ajout.
- Les boutons et formulaires vérifient à nouveau l’auteur et les permissions. Une ancienne vue, une page précédente ou une session expirée ne peut pas confirmer une nouvelle sélection. Les contrôles expirent après cinq minutes d’inactivité; les sessions ne sont pas reprises après redémarrage du gestionnaire.
- Si le module ne confirme pas l’ajout, **Vérifier l’ajout** renvoie exactement la même demande. Les changements de torrent et de critères restent bloqués pour éviter un doublon.
- Les demandes privées ne publient pas leurs notifications de progression dans les salons, même si `ALERT_CHANNEL_ID` est renseigné. **Actualiser le suivi** et `/plex operations` permettent de consulter l’état en privé. Les administrateurs conservent leurs accès aux demandes via les outils d’administration et le dashboard.
- Les discussions générales et consultations de bibliothèque déclenchées dans un salon restent des réponses de conversation normales. La recherche par messages exige toujours les salons configurés et Message Content Intent.

## Dashboard : vider tous les logs

Dans **Logs**, cliquer sur **Vider tous les logs** puis confirmer. Le bouton vide les logs manager/Plex, `bot.log` et les rotations `.1` à `.3` connues, sans fermer les fichiers ouverts par les services. Les nouveaux événements peuvent réapparaître immédiatement.

Les `.env`, SQLite, demandes, médias et sauvegardes restent intacts. L’action exige la session d’administration locale et la protection CSRF habituelle. Elle ne reçoit aucun chemin du navigateur, refuse les liens symboliques et reste limitée aux chemins de logs connus à l’intérieur du dossier de l’installation. Les journaux externes ou d’autres applications ne sont pas effacés. En cas de refus d’accès, une erreur signale que certains logs ont pu être déjà vidés.

## Mise à jour

Mettre à jour la branche `test/manager-plex-ollama`, puis redémarrer le gestionnaire, le module Plex et le dashboard. Aucun nouveau champ `.env` ni dépendance n’est nécessaire. Le redémarrage du module Plex est requis pour marquer les nouvelles notifications privées. Les demandes déjà créées auparavant conservent leur mode de notification initial.
