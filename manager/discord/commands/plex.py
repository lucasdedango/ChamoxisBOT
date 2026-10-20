import os
import discord
from manager.ai.catalog import identified_intent
from shared.schemas import AddTorrent, Preferences
from manager.discord.bridge import plex_client, manager_client
from manager.ai.selection import default_quality


class ConfirmDownload(discord.ui.View):
    def __init__(self, owner_id, link, title, prefs, request_id):
        super().__init__(timeout=180)
        self.owner_id, self.link, self.title = owner_id, link, title
        self.prefs, self.request_id = prefs, request_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Cette confirmation appartient à un autre utilisateur.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmer l’ajout", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        from manager.core.permissions import allowed_user
        if not allowed_user(interaction.user.id):
            await interaction.response.send_message("Ajout non autorisé.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not allowed_user(interaction.user.id):
            await interaction.followup.send('Ajout non autorisé : ton accès a été retiré.', ephemeral=True)
            return
        body = AddTorrent(request_id=self.request_id, link=self.link, title=self.title,
                          user_id=interaction.user.id, channel_id=None, private=True,
                          prefs=Preferences.model_validate(self.prefs), confirmed=True)
        try:
            task = await plex_client().request("POST", "/downloads", json=body.model_dump())
        except Exception:
            await interaction.followup.send("Ajout non confirmé. Tu peux réessayer avec ce même bouton sans créer une seconde tâche.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.followup.send(f"Demande enregistrée : `{task['id']}`. Le module Plex gère le téléchargement et l’import.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Demande annulée.", view=self)
        self.stop()


async def offer_download(interaction, link, title, prefs):
    # The interaction ID is stable across retries of this confirmation view.
    try:
        prefs = Preferences.model_validate(prefs).model_dump()
    except ValueError:
        await interaction.followup.send("Répertoire cible invalide : saisis un nom, sans chemin ni séparateur.", ephemeral=True)
        return
    await interaction.followup.send(f"Ajouter **{discord.utils.escape_markdown(title[:300])}** ?",
                                    view=ConfirmDownload(interaction.user.id, link, title, prefs, str(interaction.id)),
                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class NaturalRequest(discord.ui.View):
    def __init__(self, owner_id, intent):
        super().__init__(timeout=180)
        self.owner_id, self.intent = owner_id, intent

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Cette demande appartient à un autre utilisateur.", ephemeral=True)
        return False

    @discord.ui.button(label="Rechercher", style=discord.ButtonStyle.primary)
    async def search(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        from manager.discord.bot import send_torznab_results
        intent = self.intent
        query = intent["title"]
        target_name = query.replace("/", " - ").replace("\\", " - ")[:140] + (f" ({intent['year']})" if intent.get("year") else "")
        prefs = {"kind": intent["kind"], "target_name": target_name, "season": intent["season"],
                 "episode": intent["episode"], "series_mode": "single" if intent["episode"] else "complete"}
        matches = await plex_client().request("GET", "/library", params={"title": intent["title"], "kind": intent["kind"]})
        if matches["matches"]:
            await interaction.followup.send("Contenus possiblement déjà présents : " + ", ".join(matches["matches"][:10]) + ". Vérifie avant de confirmer l’ajout.", ephemeral=True)
        await send_torznab_results(interaction, query, intent["kind"], prefs=prefs,
                                   quality=intent.get("quality") or default_quality(), language=intent.get("language"),
                                   rank_preferences=True, year=intent.get("year"),
                                   season=intent["season"], episode=intent["episode"],
                                   selection_policy=True, prefer_quality=not bool(intent.get("quality")), min_seeders=intent.get("min_seeders"),
                                   query_aliases=intent.get("query_aliases"), imdb_id=intent.get("imdb_id"), tmdb_id=intent.get("tmdb_id"))


class IdentifyMedia(NaturalRequest):
    def __init__(self, owner_id, intent, choices):
        super().__init__(owner_id, intent)
        self.clear_items()
        self.choices = choices[:20]
        select = discord.ui.Select(placeholder="Choisir l’œuvre", options=[discord.SelectOption(label=(item['title'] + ' (' + str(item.get('year') or '?') + ')')[:100], value=str(index), description='Série' if item['kind'] == 'series' else 'Film') for index, item in enumerate(self.choices)])
        select.callback = self.choose
        self.add_item(select)
        self.select = select

    async def choose(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            selected = self.choices[int(self.select.values[0])]
            media = await manager_client().request("GET", f"/catalog/media/{selected['kind']}/{selected['tmdb_id']}")
            intent = identified_intent(self.intent, media)
            await interaction.edit_original_response(content=f"Œuvre identifiée : **{discord.utils.escape_markdown(media['title'])}** ({media.get('year') or '?'})\n{media['tmdb_url']}", view=NaturalRequest(self.owner_id, intent))
            self.stop()
        except Exception:
            await interaction.followup.send("La fiche TMDb est indisponible; réessaie plus tard.", ephemeral=True)


async def demande(interaction: discord.Interaction, texte: str):
    from manager.discord.private_search import PrivateSearch
    await PrivateSearch(interaction.user.id, texte, manager_client, plex_client).start(interaction)


async def download_summary(user_id):
    tasks = await plex_client().request("GET", "/tasks")
    tasks = [t for t in tasks if t["payload"]["user_id"] == user_id]
    if not tasks:
        return "Aucune demande enregistrée. Lance /recherchetorrent pour commencer."
    snapshots, note = {}, ""
    try:
        result = await plex_client().request("GET", "/downloads/status", params={"user_id": user_id})
        owned = {task['id'] for task in tasks}
        snapshots = {row['id']: row for row in result['downloads'] if row['id'] in owned}
    except Exception:
        note = "\nProgression qBittorrent temporairement indisponible ; les états des demandes restent affichés."
    states = {'queued': 'en attente', 'adding': 'ajout en cours', 'downloading': 'téléchargement', 'importing': 'import en cours', 'completed': 'terminé', 'needs_review': 'à vérifier', 'simulated': 'mode test', 'cancelled': 'annulé', 'failed': 'échec', 'duplicate': 'doublon'}
    lines = []
    for task in tasks[:12]:
        row = snapshots.get(task['id'], {})
        progress = row.get('progress')
        suffix = f" · {max(0, min(100, int(progress * 100)))}%" if isinstance(progress, (float, int)) else ''
        lines.append(f"**{discord.utils.escape_markdown(task['payload'].get('title', '')[:70])}** — {states.get(task['state'], task['state'])}{suffix}\n`{task['id']}`")
        if row.get('observation'):
            lines.append(discord.utils.escape_markdown(row['observation'][:160]))
    return "\n".join(lines)[:1600] + note


class DownloadStatusView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    @discord.ui.button(label='Actualiser le suivi', style=discord.ButtonStyle.primary)
    async def refresh(self, interaction, button):
        from manager.core.permissions import allowed_user
        if interaction.user.id != self.owner_id or not allowed_user(self.owner_id):
            await interaction.response.send_message('Suivi non autorisé.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            content = await download_summary(self.owner_id)
        except Exception:
            content = 'Impossible de consulter tes demandes actuellement. Réessaie après le redémarrage du module Plex.'
        await interaction.edit_original_response(content=content, view=self, allowed_mentions=discord.AllowedMentions.none())


async def operations(interaction: discord.Interaction):
    from manager.core.permissions import allowed_user
    if not allowed_user(interaction.user.id):
        await interaction.response.send_message('Suivi non autorisé.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        content = await download_summary(interaction.user.id)
    except Exception:
        content = 'Impossible de consulter tes demandes actuellement. Réessaie après le redémarrage du module Plex.'
    await interaction.followup.send(content, view=DownloadStatusView(interaction.user.id), ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())
