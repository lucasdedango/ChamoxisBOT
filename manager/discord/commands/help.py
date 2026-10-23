"""Small, private help pages shared by the slash command and its menu."""
import discord
from discord import app_commands

PAGES = {
    'Démarrer': ('Bienvenue sur ChamoxisBOT',
        '**/recherchetorrent** : trouver un film ou une série.\n'
        '**/telechargements** : consulter tes demandes et leurs états.\n'
        '**/ajoutertorrent** : fournir ton propre magnet ou fichier .torrent.\n'
        '**/ia** : discuter en privé avec l’IA.\n\n'
        'Exemple : `/recherchetorrent query:Les Anneaux de pouvoir S01 en 1080p`.\n'
        'Aucune inscription via /info n’est nécessaire. L’accès dépend des autorisations du bot.'),
    'Rechercher': ('Choisir une œuvre et un torrent',
        '1. Écris un titre ou une phrase dans **/recherchetorrent** ; sans texte, utilise le formulaire.\n'
        '2. Vérifie **J’ai compris**. Clique sur un titre pour consulter sa fiche TMDb, puis choisis l’œuvre dans le menu.\n'
        '3. Choisis la saison entière ou un épisode. La date TMDb indique les saisons à venir.\n'
        '4. Choisis un torrent dans le menu, puis **Confirmer l’ajout**.\n\n'
        'Les menus ont 20 entrées par page. Le lien TMDb reste disponible après le choix. **Corriger la recherche** modifie ta demande. '
        '**Annuler** ferme la recherche. Choisir une œuvre ou un torrent ne lance pas un téléchargement.\n'
        'Résultats privés ; menus valables cinq minutes d’inactivité.'),
    'Messages et discussion': ('Parler au bot',
        'Dans un salon autorisé :\n'
        '• `man cherche Les Anneaux de pouvoir S01 en 1080p`\n'
        '• `bot ajoute Charlie et la Chocolaterie 2005`\n'
        '• `man pourquoi mon dernier téléchargement est bloqué ?`\n'
        '• `bot quelles séries de science-fiction sont disponibles sur Plex ?`\n\n'
        'Clique **Continuer en privé** pour la recherche. Ton message et le bouton restent visibles '
        'dans le salon ; les choix suivants sont privés. Les discussions ordinaires restent publiques. '
        'Utilise **/ia** pour une discussion privée. « cherche » et « ajoute » suivent le même parcours.'),
    'Comprendre un résultat': ('Qualité, disponibilité et problèmes',
        'Qualité demandée, ou 1080p par défaut selon la configuration. MULTI privilégié, seeds positifs '
        'privilégiés puis taille réduite ; AV1 seulement si aucun autre codec compatible n’est trouvé, avec avertissement. Un torrent sans seed peut être proposé avec '
        'avertissement : l’attente peut être longue et le téléchargement n’est pas garanti.\n\n'
        'Le bilan distingue les résultats reçus et ceux retenus. Plusieurs exclusions peuvent concerner '
        'un même torrent. Vérifie le titre, l’année, la saison et la qualité ; utilise **Corriger la recherche**. '
        'Une panne de service est signalée séparément : utilise **Réessayer**.\n\n'
        'Une date de première diffusion passée ne garantit pas qu’une saison complète soit disponible. '
        'Consulte **/telechargements** pour suivre tes demandes ; un ajout incertain se vérifie avec le même bouton. '
        'La progression reste privée ; la fin d’import est annoncée dans le salon de nouveautés configuré. '
        'Une actualisation Plex échouée ou non confirmée est signalée sans promettre que le contenu est déjà visible.'),
    'Administration': ('Accès et administration',
        'Réservé aux identifiants **DISCORD_ADMIN_IDS** configurés :\n'
        '• `/admin utilisateurs ajouter membre:@membre`\n'
        '• `/admin utilisateurs retirer membre:@membre`\n'
        '• `/admin utilisateurs liste`\n'
        '• `/admin utilisateurs mode valeur:restreint` : limiter l’accès aux utilisateurs autorisés.\n'
        '• `/admin torrents` : vue globale.\n'
        '• `/admin annuler_suivis` : confirmation avant d’annuler les suivis actifs.\n'
        '• `/admin services` et `/admin relancer`.\n\n'
        'Les accès sont persistants, prennent effet immédiatement et ne donnent aucun droit admin. '
        'En mode ouvert, tout le monde sauf les utilisateurs retirés a accès. Les salons autorisés restent requis.'),
}


def page(name):
    title, content = PAGES[name]
    return discord.Embed(title=title, description=content, colour=discord.Colour.blurple())


class HelpView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        menu = discord.ui.Select(placeholder='Choisir une rubrique du guide', options=[discord.SelectOption(label=name) for name in PAGES])
        async def choose(interaction):
            await interaction.response.edit_message(embed=page(menu.values[0]), view=self)
        menu.callback = choose
        self.add_item(menu)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message('Ouvre ton propre guide avec /aide.', ephemeral=True)
        return False


def register(tree):
    @tree.command(name='aide', description='Guide privé : recherche, téléchargements, discussion et dépannage')
    async def aide(interaction: discord.Interaction):
        await interaction.response.send_message(embed=page('Démarrer'), view=HelpView(interaction.user.id), ephemeral=True)
