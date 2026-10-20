import discord
from discord import app_commands
from manager.discord.bridge import manager_client


def register(tree):
    @tree.command(name="ia", description="Discuter en privé avec l’IA locale")
    async def chat(interaction: discord.Interaction, question: str):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await manager_client().request("POST", "/ai/chat", json={"messages": [{"role": "user", "content": question}], "structured": False})
            await interaction.followup.send(str(result["result"])[:1800], ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            await interaction.followup.send("Aucun modèle IA disponible actuellement.", ephemeral=True)
