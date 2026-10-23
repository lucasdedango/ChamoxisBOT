"""Explicit administrator-only commands and persistent user access."""
import asyncio
import time
import discord
from discord import app_commands
from manager.core.permissions import administrator, access_snapshot, update_access
from manager.discord.bridge import plex_client
from . import server


async def require_admin(interaction):
    if administrator(interaction.user.id):
        return True
    await interaction.response.send_message('Commande réservée aux administrateurs configurés.', ephemeral=True)
    return False


class CancelTracking(discord.ui.View):
    def __init__(self, owner_id, task_ids):
        super().__init__(timeout=120)
        self.owner_id, self.task_ids = owner_id, tuple(task_ids)
        self.lock, self.finished = asyncio.Lock(), False
        self.expires = time.monotonic() + 120

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or not administrator(interaction.user.id):
            await interaction.response.send_message('Confirmation réservée à l’administrateur qui l’a demandée.', ephemeral=True)
            return False
        if self.finished or time.monotonic() >= self.expires:
            await interaction.response.send_message('Cette confirmation est expirée ou déjà traitée.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Confirmer l’annulation des suivis', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        async with self.lock:
            if self.finished:
                return
            cancelled = 0
            try:
                for task_id in self.task_ids:
                    if not administrator(interaction.user.id):
                        raise PermissionError('Administrator access revoked')
                    task = await plex_client().request('GET', '/tasks/' + task_id)
                    if task['state'] in {'queued', 'downloading', 'needs_review'}:
                        await plex_client().request('POST', f'/tasks/{task_id}/cancel')
                        cancelled += 1
            except Exception:
                await interaction.followup.send('Annulation partielle ou non confirmée. Vérifie les suivis avant de réessayer. Les fichiers et torrents ne sont pas supprimés.', ephemeral=True)
                return
            self.finished = True
            await interaction.edit_original_response(content=f'{cancelled} suivi(s) annulé(s). Les torrents et fichiers existants sont conservés.', view=None)
            self.stop()

    @discord.ui.button(label='Conserver les suivis')
    async def cancel(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        if self.lock.locked():
            await interaction.response.send_message('L’annulation des suivis est déjà en cours.', ephemeral=True)
            return
        self.finished = True
        self.stop()
        await interaction.response.edit_message(content='Aucun suivi annulé.', view=None)

    async def on_timeout(self):
        self.finished = True


def register(tree, torrents_callback):
    group = app_commands.Group(name='admin', description='Administration du bot et des accès', guild_only=True)
    users = app_commands.Group(name='utilisateurs', description='Gérer les accès au bot', parent=group)

    @users.command(name='ajouter', description='Autoriser un membre à utiliser le bot, sans droits admin')
    async def ajouter(interaction: discord.Interaction, membre: discord.Member):
        if not await require_admin(interaction):
            return
        if membre.bot or administrator(membre.id):
            await interaction.response.send_message('Choisis un utilisateur humain non administrateur ; les administrateurs ont déjà accès.', ephemeral=True)
            return
        settings = update_access(interaction.user.id, membre.id, allow=True)
        note = ' Le mode est ouvert : utilise /admin utilisateurs mode pour restreindre l’accès.' if settings['mode'] == 'open' else ''
        await interaction.response.send_message(f'Utilisateur `{membre.id}` autorisé. Effet immédiat, y compris pour les messages bot/man et les commandes privées.{note}', ephemeral=True)

    @users.command(name='retirer', description='Retirer l’accès d’un membre, y compris aux confirmations en cours')
    async def retirer(interaction: discord.Interaction, membre: discord.Member):
        if not await require_admin(interaction):
            return
        if administrator(membre.id):
            await interaction.response.send_message('Cet administrateur conserve son accès configuré dans DISCORD_ADMIN_IDS.', ephemeral=True)
            return
        update_access(interaction.user.id, membre.id, allow=False)
        await interaction.response.send_message(f'Accès retiré à `{membre.id}`. Les demandes déjà confirmées ne sont pas annulées.', ephemeral=True)

    @users.command(name='liste', description='Consulter le mode d’accès, les autorisés et les retraits')
    async def liste(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        settings = access_snapshot()
        lines = ['Mode : ' + ('ouvert (sauf retraits)' if settings['mode'] == 'open' else 'restreint')]
        for key, label in [('admins', 'Administrateurs configurés'), ('configured', 'Autorisés via .env'), ('added', 'Ajoutés via Discord'), ('blocked', 'Accès retirés')]:
            values = settings[key]
            text = ', '.join(str(value) for value in values[:30]) or 'aucun'
            lines.append(label + ' : ' + text + (f' (+{len(values)-30})' if len(values) > 30 else ''))
        await interaction.response.send_message('\n'.join(lines)[:1900], ephemeral=True)

    @users.command(name='mode', description='Choisir un accès ouvert ou réservé aux utilisateurs autorisés')
    @app_commands.choices(valeur=[app_commands.Choice(name='restreint', value='restricted'), app_commands.Choice(name='ouvert', value='open')])
    async def mode(interaction: discord.Interaction, valeur: str):
        if not await require_admin(interaction):
            return
        update_access(interaction.user.id, mode=valeur)
        text = 'Accès limité aux administrateurs et utilisateurs autorisés.' if valeur == 'restricted' else 'Accès ouvert à tous, sauf aux utilisateurs dont l’accès a été retiré.'
        await interaction.response.send_message(text + ' Les restrictions de salons restent appliquées.', ephemeral=True)

    @group.command(name='torrents', description='Voir les derniers torrents de tous les utilisateurs')
    async def torrents(interaction: discord.Interaction):
        if await require_admin(interaction):
            await torrents_callback(interaction)

    @group.command(name='annuler_suivis', description='Préparer l’annulation des suivis actifs, sans supprimer de fichiers')
    async def annuler_suivis(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        tasks = await plex_client().request('GET', '/tasks')
        ids = [task['id'] for task in tasks if task['state'] in {'queued', 'downloading', 'needs_review'}]
        if not ids:
            await interaction.followup.send('Aucun suivi actif à annuler.', ephemeral=True)
            return
        await interaction.followup.send(f'Annuler les {len(ids)} suivis actifs actuels ? Les téléchargements qBittorrent et les fichiers seront conservés. Les nouveaux suivis ne sont pas concernés.', view=CancelTracking(interaction.user.id, ids), ephemeral=True)

    server.register(group)
    tree.add_command(group)
