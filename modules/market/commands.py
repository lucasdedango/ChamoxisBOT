"""Commands for the separate Market Discord application."""
import time
from uuid import uuid4
import discord
from discord import app_commands
from . import runtime as rt


async def permitted(interaction, admin=False):
    if interaction.channel_id != rt.DISCORD_CHANNEL_ID:
        await interaction.response.send_message('Utilise le salon configuré pour Market.', ephemeral=True)
        return False
    check = rt.ACCESS.administrator if admin else rt.ACCESS.allowed
    if not check(interaction.user.id):
        await interaction.response.send_message('Accès Market non autorisé.' if not admin else 'Commande réservée aux administrateurs Market.', ephemeral=True)
        return False
    return True


def visible(user_id):
    return [search for search in rt.SEARCHES if search.get('owner_id') == user_id or rt.ACCESS.administrator(user_id)]


def owned(user_id, identifier):
    return next((search for search in visible(user_id) if search['id'] == identifier), None)


async def require_search(interaction, identifier):
    if not await permitted(interaction):
        return None
    search = owned(interaction.user.id, identifier)
    if search is None:
        await interaction.response.send_message('Recherche introuvable ou non autorisée. Consulte /recherche liste.', ephemeral=True)
    return search


def intervals(minimum, maximum):
    low, high = rt.validate_interval(minimum, maximum)
    if low < float(rt.CONFIG['minimum_interval_minutes']):
        raise ValueError(f"Intervalle minimum configuré : {rt.CONFIG['minimum_interval_minutes']:g} minutes.")
    return low, high


class SearchList(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=180)
        self.owner_id, self.page = owner_id, 0

    def embed(self):
        rows = visible(self.owner_id)
        self.page = min(self.page, max(0, (len(rows)-1)//10))
        lines = []
        for search in rows[self.page*10:(self.page+1)*10]:
            state = rt.state_for(rt.STATE, search['id'])
            icon = '⏸️' if not rt.search_is_active(search) else '🔴' if state.get('blocked') else '🟢'
            lines.append(f"{icon} **{discord.utils.escape_markdown(search['name'])}** · {search['site']}\n"
                         f"`{search['id']}` · {search['min_minutes']:g}–{search['max_minutes']:g} min")
        self.previous.disabled = self.page == 0
        self.next.disabled = (self.page+1)*10 >= len(rows)
        embed = discord.Embed(title='Recherches Market', description='\n\n'.join(lines) or 'Aucune recherche. Utilise /recherche ajouter.')
        embed.set_footer(text=f'Page {self.page+1}/{max(1, (len(rows)+9)//10)} · /recherche details pour le lien et le suivi')
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('Ouvre ta propre liste avec /recherche liste.', ephemeral=True)
            return False
        return await permitted(interaction)

    @discord.ui.button(label='Précédent')
    async def previous(self, interaction, button):
        self.page = max(0, self.page-1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='Suivant')
    async def next(self, interaction, button):
        self.page += 1
        await interaction.response.edit_message(embed=self.embed(), view=self)


class DeleteSearch(discord.ui.View):
    def __init__(self, owner_id, identifier):
        super().__init__(timeout=120)
        self.owner_id, self.identifier, self.done = owner_id, identifier, False
        self.expires = time.monotonic() + 120

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or self.done or time.monotonic() >= self.expires:
            await interaction.response.send_message('Cette confirmation est expirée ou appartient à un autre utilisateur.', ephemeral=True)
            return False
        return await permitted(interaction)

    @discord.ui.button(label='Supprimer la recherche', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        search = owned(interaction.user.id, self.identifier)
        if search is None:
            await interaction.response.send_message('Recherche introuvable ou accès retiré.', ephemeral=True)
            return
        # Save before changing the in-memory list; a failed write does not remove the search.
        searches = [row for row in rt.SEARCHES if row['id'] != self.identifier]
        rt.save_json(rt.SEARCHES_PATH, searches)
        rt.SEARCHES[:] = searches
        rt.STATE.get('searches', {}).pop(self.identifier, None)
        rt.save_json(rt.STATE_PATH, rt.STATE)
        rt.next_due.pop(self.identifier, None)
        self.done = True
        self.stop()
        await interaction.response.edit_message(content='Recherche supprimée. Les annonces Discord déjà envoyées sont conservées.', view=None)

    @discord.ui.button(label='Conserver')
    async def cancel(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content='Recherche conservée.', view=None)


def register(tree):
    searches = app_commands.Group(name='recherche', description='Tes surveillances Leboncoin et Vinted', guild_only=True)

    @searches.command(name='ajouter', description='Surveiller une URL de recherche Leboncoin ou Vinted')
    @app_commands.describe(url='URL copiée après avoir réglé les filtres sur le site', nom='Nom de la recherche',
                           minimum='Intervalle minimum en minutes', maximum='Intervalle maximum en minutes')
    async def ajouter(interaction: discord.Interaction, url: str, nom: str, minimum: float = 10.0, maximum: float = 20.0):
        if not await permitted(interaction):
            return
        try:
            site, url = rt.validate_search_url(url)
            low, high = intervals(minimum, maximum)
            if not nom.strip() or len(nom) > 80:
                raise ValueError('Le nom doit contenir de 1 à 80 caractères.')
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        own = [row for row in rt.SEARCHES if row.get('owner_id') == interaction.user.id]
        if len(own) >= int(rt.CONFIG['max_searches_per_user']):
            await interaction.response.send_message('Limite de recherches atteinte. Supprime une recherche ou contacte un administrateur.', ephemeral=True)
            return
        if any(row['url'] == url for row in own):
            await interaction.response.send_message('Tu surveilles déjà cette URL.', ephemeral=True)
            return
        search = {'id': f'{site}-{uuid4().hex[:10]}', 'owner_id': interaction.user.id, 'name': nom.strip(),
                  'site': site, 'url': url, 'min_minutes': low, 'max_minutes': high, 'enabled': True}
        rt.save_json(rt.SEARCHES_PATH, [*rt.SEARCHES, search])
        rt.SEARCHES.append(search)
        rt.next_due[search['id']] = time.monotonic() + 1
        await interaction.response.send_message(f"Recherche `{search['id']}` ajoutée : {low:g}–{high:g} min. Le premier passage mémorise les annonces existantes sans les publier. Les nouvelles annonces seront publiées dans ce salon.", ephemeral=True)

    @searches.command(name='liste', description='Lister tes recherches et leurs identifiants')
    async def liste(interaction: discord.Interaction):
        if not await permitted(interaction):
            return
        view = SearchList(interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @searches.command(name='details', description='Voir le lien et l’état d’une recherche')
    async def details(interaction: discord.Interaction, identifiant: str):
        search = await require_search(interaction, identifiant)
        if not search:
            return
        state = rt.state_for(rt.STATE, identifiant)
        delay = rt.due_in_minutes(identifiant)
        text = (f"**{discord.utils.escape_markdown(search['name'])}** · `{identifiant}`\n<{search['url']}>\n"
                f"Propriétaire : `{search['owner_id']}` · {'active' if rt.search_is_active(search) else 'suspendue'}\n"
                f"Intervalle : {search['min_minutes']:g}–{search['max_minutes']:g} min\n"
                f"Dernier contrôle : {state.get('last_check') or 'pas encore effectué'}\n"
                f"Blocage/vérification : {'oui' if state.get('blocked') else 'non'}\n"
                f"Prochain contrôle : {f'environ {delay:.1f} min' if delay is not None else 'non planifié'}")
        await interaction.response.send_message(embed=discord.Embed(title='Détails de la recherche', description=text), ephemeral=True)

    @searches.command(name='intervalle', description='Changer le délai aléatoire entre deux contrôles')
    async def intervalle(interaction: discord.Interaction, identifiant: str, minimum: float, maximum: float):
        search = await require_search(interaction, identifiant)
        if not search:
            return
        try:
            low, high = intervals(minimum, maximum)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        updated = [{**row, 'min_minutes': low, 'max_minutes': high} if row['id'] == identifiant else row for row in rt.SEARCHES]
        rt.save_json(rt.SEARCHES_PATH, updated)
        search.update(min_minutes=low, max_minutes=high)
        previous_due = rt.next_due.get(identifiant, 0)
        rt.schedule_next(search)
        if rt.state_for(rt.STATE, identifiant).get('blocked'):
            rt.next_due[identifiant] = max(previous_due, rt.next_due[identifiant])
        delay = max(0, rt.next_due[identifiant] - time.monotonic())
        await interaction.response.send_message(f'Intervalle modifié ; prochain contrôle dans environ {delay/60:.1f} min. Une attente imposée par le site est conservée.', ephemeral=True)

    async def switch(interaction, identifiant, enabled):
        search = await require_search(interaction, identifiant)
        if not search:
            return
        updated = [{**row, 'enabled': enabled} if row['id'] == identifiant else row for row in rt.SEARCHES]
        rt.save_json(rt.SEARCHES_PATH, updated)
        search['enabled'] = enabled
        if enabled:
            rt.next_due[identifiant] = max(rt.next_due.get(identifiant, 0), time.monotonic()+1)
        await interaction.response.send_message('Recherche reprise.' if enabled else 'Recherche mise en pause ; une lecture déjà en cours peut se terminer mais ne publiera plus d’annonce.', ephemeral=True)

    @searches.command(name='pause', description='Suspendre une recherche')
    async def pause(interaction: discord.Interaction, identifiant: str):
        await switch(interaction, identifiant, False)

    @searches.command(name='reprendre', description='Réactiver une recherche')
    async def reprendre(interaction: discord.Interaction, identifiant: str):
        await switch(interaction, identifiant, True)

    @searches.command(name='supprimer', description='Supprimer une recherche après confirmation')
    async def supprimer(interaction: discord.Interaction, identifiant: str):
        if await require_search(interaction, identifiant):
            await interaction.response.send_message('Supprimer cette recherche ?', view=DeleteSearch(interaction.user.id, identifiant), ephemeral=True)

    @searches.command(name='verifier', description='Demander un contrôle supplémentaire, sans contourner un blocage du site')
    async def verifier(interaction: discord.Interaction, identifiant: str):
        search = await require_search(interaction, identifiant)
        if not search:
            return
        state = rt.state_for(rt.STATE, identifiant)
        now = time.monotonic()
        if not rt.search_is_active(search) or state.get('blocked') and rt.next_due.get(identifiant, 0) > now:
            await interaction.response.send_message('Recherche suspendue ou en attente après un blocage. Attends la reprise prévue.', ephemeral=True)
            return
        if now - state.get('last_manual_check', -1e10) < float(rt.CONFIG['manual_check_cooldown_seconds']) or rt.search_lock.locked():
            await interaction.response.send_message('Un contrôle est déjà en cours ou vient d’être demandé. Réessaie plus tard.', ephemeral=True)
            return
        state['last_manual_check'] = now
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with rt.search_lock:
            ok, message = await rt.run_search(search, manual=True)
            if ok:
                rt.schedule_next(search)
        await interaction.followup.send(('✅ ' if ok else '⚠️ ') + message[:500], ephemeral=True)

    async def autocomplete(interaction, current):
        if not rt.ACCESS.allowed(interaction.user.id) or interaction.channel_id != rt.DISCORD_CHANNEL_ID:
            return []
        return [app_commands.Choice(name=f"{row['name']} ({row['id']})"[:100], value=row['id']) for row in visible(interaction.user.id)
                if current.casefold() in (row['name']+' '+row['id']).casefold()][:25]
    for name in ('details', 'intervalle', 'pause', 'reprendre', 'supprimer', 'verifier'):
        searches.get_command(name).autocomplete('identifiant')(autocomplete)
    tree.add_command(searches)

    admin = app_commands.Group(name='admin', description='Administration du bot Market', guild_only=True)
    users = app_commands.Group(name='utilisateurs', description='Accès indépendants du bot Plex', parent=admin)

    @users.command(name='ajouter', description='Autoriser un utilisateur à gérer ses recherches Market')
    async def autoriser(interaction: discord.Interaction, membre: discord.Member):
        if not await permitted(interaction, admin=True):
            return
        if membre.bot or rt.ACCESS.administrator(membre.id):
            await interaction.response.send_message('Choisis un utilisateur humain non administrateur Market.', ephemeral=True)
            return
        rt.ACCESS.set_user(interaction.user.id, membre.id, True)
        await interaction.response.send_message(f'Accès Market accordé à `{membre.id}`. Aucun droit admin accordé.', ephemeral=True)

    @users.command(name='retirer', description='Retirer l’accès et suspendre les recherches de cet utilisateur')
    async def retirer(interaction: discord.Interaction, membre: discord.Member):
        if not await permitted(interaction, admin=True):
            return
        if rt.ACCESS.administrator(membre.id):
            await interaction.response.send_message('Cet administrateur garde son accès configuré dans MARKET_ADMIN_IDS.', ephemeral=True)
            return
        rt.ACCESS.set_user(interaction.user.id, membre.id, False)
        await interaction.response.send_message('Accès retiré. Ses recherches sont suspendues et conservées ; un nouvel ajout d’accès les réactivera si elles n’étaient pas en pause.', ephemeral=True)

    @users.command(name='liste', description='Lister les administrateurs, autorisés et retraits Market')
    async def utilisateurs(interaction: discord.Interaction):
        if not await permitted(interaction, admin=True):
            return
        allowed = (rt.ACCESS.configured | set(rt.ACCESS.state['added'])) - set(rt.ACCESS.state['blocked'])
        text = f"Administrateurs Market : {', '.join(str(x) for x in sorted(rt.ACCESS.admins)) or 'aucun'}\n"
        text += f"Utilisateurs autorisés : {', '.join(str(x) for x in sorted(allowed)) or 'aucun'}\n"
        text += f"Accès retirés : {', '.join(str(x) for x in rt.ACCESS.state['blocked']) or 'aucun'}"
        await interaction.response.send_message(text[:1900], ephemeral=True)

    @admin.command(name='leboncoin_verifier', description='Ouvrir ou réutiliser la fenêtre serveur pour une vérification humaine')
    async def leboncoin(interaction: discord.Interaction, identifiant: str):
        if not await permitted(interaction, admin=True):
            return
        search = rt.get_search(identifiant)
        if not search or search['site'] != 'leboncoin' or not rt.search_is_active(search):
            await interaction.response.send_message('Choisis une recherche Leboncoin active et autorisée.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        started = rt._start_lbc_manual_verification(search, rt.CONFIG)
        await interaction.followup.send('Fenêtre navigateur demandée sur le PC du bot.' if started else 'La session est déjà ouverte. Effectue la vérification dans la fenêtre du PC du bot.', ephemeral=True)
    leboncoin.autocomplete('identifiant')(autocomplete)
    tree.add_command(admin)

    @tree.command(name='etat', description='Voir l’état de tes surveillances Market')
    async def etat(interaction: discord.Interaction):
        if not await permitted(interaction):
            return
        rows = visible(interaction.user.id)
        text = f"{len(rows)} recherche(s) visible(s), {sum(rt.search_is_active(row) for row in rows)} active(s).\n"
        text += 'Consulte /recherche liste puis /recherche details pour les blocages et le prochain contrôle.'
        await interaction.response.send_message(text, ephemeral=True)

    @tree.command(name='aide', description='Guide du bot Market : recherches, accès et vérifications')
    async def aide(interaction: discord.Interaction):
        await interaction.response.send_message(
            '**Market — Leboncoin & Vinted**\nCopie l’URL de recherche après avoir réglé prix, mots-clés et filtres sur le site.\n'
            '`/recherche ajouter url:… nom:… minimum:10 maximum:20`\n'
            '`/recherche liste` · `details` · `intervalle` · `pause` · `reprendre` · `verifier` · `supprimer`\n'
            'Tu gères uniquement tes recherches. Les premières annonces sont mémorisées sans publication ; les nouvelles sont envoyées dans le salon Market.\n'
            'Un blocage du site entraîne une attente ; une vérification Leboncoin se fait manuellement sur le PC du bot.\n'
            '**Admin Market** : `/admin utilisateurs ajouter`, `retirer`, `liste` et `/admin leboncoin_verifier`.\n'
            'Les autorisations et le token sont distincts de ChamoxisBOT/Plex.', ephemeral=True)
