import asyncio
import logging
import os
import discord
from chamoxis_common.logging import redact

logger = logging.getLogger(__name__)


def allowed_channel(channel_id):
    configured = os.getenv("DISCORD_NOTIFICATION_CHANNEL_IDS", os.getenv("ALERT_CHANNEL_ID", ""))
    return channel_id in {int(value.strip()) for value in configured.split(",") if value.strip()}


def notification_text(event):
    data = event.get("data", {})
    kind = event["type"]
    if kind == "notification.requested":
        return redact(str(data.get("text", "")))[:1800]
    templates = {
        "torrent.added": "Téléchargement ajouté",
        "torrent.completed": "Téléchargement terminé",
        "torrent.progress": "Téléchargement en cours",
        "torrent.failed": "Téléchargement à vérifier",
        "torrent.simulated": "Mode test : aucun téléchargement ni import",
        "plex.import.started": "Import Plex en cours",
        "plex.import.completed": "Import Plex terminé",
        "plex.import.failed": "Import Plex à vérifier",
        "service.changed": "État d’un service modifié",
    }
    if kind not in templates:
        return None
    details = data.get("message") or data.get("reason") or data.get("name") or data.get("status", "")
    if kind == "torrent.progress":
        details = f"{data.get('name', '')} — {int(float(data.get('progress', 0)) * 100)}% — {int(data.get('dlspeed', 0)) // 1024} KiB/s"
    return redact(f"{templates[kind]} : {details}\nTâche : {event.get('task_id') or event['id']}")[:1800]


class Notifications:
    def __init__(self, store, bot):
        self.store, self.bot = store, bot

    async def step(self):
        for event in self.store.pending_events():
            channel_id = event.get("channel_id") or int(os.getenv("ALERT_CHANNEL_ID", "0") or 0)
            text = notification_text(event)
            if not text or not channel_id or not allowed_channel(channel_id):
                self.store.delivered(event["id"])
                continue
            if not self.bot.is_ready():
                self.store.retry(event["id"])
                continue
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                message_key = f"progress_message:{channel_id}:{event.get('task_id')}"
                existing = self.store.get(message_key) if event.get("task_id") else None
                if existing:
                    try:
                        message = await channel.fetch_message(existing)
                        await message.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
                    except discord.NotFound:
                        message = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                else:
                    message = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                if event.get("task_id"):
                    self.store.set(message_key, message.id)
                self.store.delivered(event["id"])
            except Exception:
                logger.warning("Discord notification failed for event %s", event["id"])
                self.store.retry(event["id"])

    async def run(self):
        while True:
            await self.step()
            await asyncio.sleep(2)
