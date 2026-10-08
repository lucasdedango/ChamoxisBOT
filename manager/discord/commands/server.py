import os
import discord
from discord import app_commands
from chamoxis_common.http import APIClient
from manager.core.permissions import administrator
from manager.discord.bridge import manager_client


def register(tree):
    group = app_commands.Group(name="server", description="État des services du serveur")

    @group.command(name="status", description="Consulter l’état des services")
    async def status(interaction: discord.Interaction):
        if not administrator(interaction.user.id):
            await interaction.response.send_message("Commande réservée aux administrateurs configurés.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        items = await manager_client().request("GET", "/services")
        await interaction.followup.send("\n".join(f"{s['name']} : {s['status']} ({s['attempts']} relances)" for s in items)[:1800] or "Aucun service configuré.", ephemeral=True)

    @group.command(name="restart", description="Relancer un service explicitement géré")
    async def restart(interaction: discord.Interaction, service: str):
        if not administrator(interaction.user.id):
            await interaction.response.send_message("Commande réservée aux administrateurs configurés.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        client = APIClient(os.getenv("MANAGER_URL", "http://127.0.0.1:8760"), os.getenv("MANAGER_ADMIN_API_KEY", ""))
        from urllib.parse import quote
        try:
            await client.request("POST", "/services/" + quote(service, safe="") + "/restart")
            await interaction.followup.send("Relance demandée. Consulte `/server status` pour vérifier la santé.", ephemeral=True)
        except Exception:
            await interaction.followup.send("Relance refusée ou impossible. Consulte la configuration et les logs.", ephemeral=True)
    tree.add_command(group)
