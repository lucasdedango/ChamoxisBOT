"""Opt-in, per-user conversations with explicit confirmation of a proposed torrent."""
import asyncio
import logging
import re
import time
import discord
from manager.core.permissions import allowed_user, ids
from manager.discord.bridge import manager_client, plex_client
from shared.schemas import AddTorrent, Preferences

logger = logging.getLogger(__name__)


class Conversation:
    def __init__(self, store, manager_factory=manager_client, plex_factory=plex_client, clock=time.time):
        self.store, self.manager_factory, self.plex_factory, self.clock = store, manager_factory, plex_factory, clock
        self.locks = {}
        self.lock_users = {}

    async def say(self, message, text):
        return await message.channel.send(text[:1950], allowed_mentions=discord.AllowedMentions.none())

    def save(self, key, state):
        self.store.set(key, {**state, "expires": self.clock() + 300})

    async def handle(self, message):
        if message.author.bot or message.guild is None or message.channel.id not in ids("DISCORD_CONVERSATION_CHANNEL_IDS"):
            return
        if not allowed_user(message.author.id):
            return
        key = f"conversation:{message.guild.id}:{message.channel.id}:{message.author.id}"
        lock = self.locks.setdefault(key, asyncio.Lock())
        self.lock_users[key] = self.lock_users.get(key, 0) + 1
        try:
            async with lock:
                await self.respond(message, key)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Conversation failed user=%s channel=%s error=%s", message.author.id, message.channel.id, type(error).__name__)
            await self.say(message, "Je n’ai pas pu terminer la demande. Réessaie avec « bot cherche … » ou `/recherchetorrent`.")
        finally:
            self.lock_users[key] -= 1
            if not self.lock_users[key]:
                self.lock_users.pop(key)
                self.locks.pop(key)

    async def respond(self, message, key):
        text = message.content.strip()
        trigger = re.match(r"^bot\s+cherche\b\s*[:,]?\s*(.*)$", text, re.I | re.S)
        state = self.store.get(key)
        if state and state.get("expires", 0) < self.clock():
            self.store.set(key, None)
            state = None
        if trigger:
            request = trigger[1].strip(' "«»')
            if not request:
                self.save(key, {"phase": "title"})
                await self.say(message, "Quel film ou quelle série veux-tu chercher ?")
                return
            await self.search(message, key, request)
            return
        if not state:
            return
        answer = text.casefold().strip(" .!")
        if answer in {"non", "annule", "annuler", "stop"}:
            self.store.set(key, None)
            note = " Une demande déjà confirmée peut être en cours : consulte `/plex operations`." if state.get("uncertain") else ""
            await self.say(message, "Conversation annulée." + note)
            return
        if state["phase"] in {"title", "clarify"}:
            if answer in {"oui", "oui merci", "ok", "okay"}:
                await self.say(message, "Précise le titre ou réponds à ma question ; rien n’a été ajouté.")
                return
            request = text if state["phase"] == "title" else state["request"] + "; précision : " + text
            await self.search(message, key, request)
            return
        if state["phase"] != "confirm":
            return
        reference = getattr(message, "reference", None)
        if reference and reference.message_id != state["proposal_message_id"]:
            return
        # A "yes" sent before the proposal existed cannot confirm that proposal.
        if message.id <= state["proposal_message_id"]:
            return
        if answer.isdigit():
            if state.get("uncertain"):
                await self.say(message, "L’ajout précédent est incertain. Réponds « oui » pour vérifier cette même demande, ou consulte `/plex operations`.")
                return
            index = int(answer) - 1
            if not 0 <= index < len(state["items"]):
                await self.say(message, f"Choisis un numéro entre 1 et {len(state['items'])}.")
                return
            state["index"] = index
            state["request_id"] = f"conversation:{state['origin_id']}:{index}"
            await self.propose(message, key, state)
            return
        if answer not in {"oui", "oui merci", "oui ajoute", "oui ajoute le", "confirme", "je confirme"}:
            return
        prefs = state["prefs"]
        item = state["items"][state["index"]]
        body = AddTorrent(request_id=state["request_id"], link=item.get("enclosure") or item["link"],
                          title=item["title"], user_id=message.author.id, channel_id=message.channel.id,
                          prefs=Preferences.model_validate(prefs), confirmed=True)
        try:
            task = await self.plex_factory().request("POST", "/downloads", json=body.model_dump())
        except Exception:
            self.save(key, {**state, "uncertain": True})
            await self.say(message, "Je n’ai pas reçu la confirmation du module Plex. Réponds encore « oui » pour vérifier la même demande sans la créer en double.")
            return
        self.store.set(key, None)
        await self.say(message, f"Demande enregistrée : `{task['id']}`. Le module Plex gère la suite ; consulte `/plex operations`. En mode test, rien ne sera téléchargé.")

    async def search(self, message, key, request):
        if len(request) > 1800:
            await self.say(message, "Raccourcis ta demande, puis recommence avec « bot cherche … ».")
            return
        await self.say(message, "Je prépare la recherche…")
        self.store.set(key, None)
        analysis = await self.manager_factory().request("POST", "/ai/analyze", json={"text": "Ajoute " + request})
        intent = analysis["intent"]
        if intent.get("clarification"):
            self.save(key, {"phase": "clarify", "request": request})
            await self.say(message, intent["clarification"])
            return
        prefs = Preferences(kind=intent["kind"], target_name=intent["title"] + (f" ({intent['year']})" if intent.get("year") else ""),
                            season=intent.get("season", 0), episode=intent.get("episode", 0),
                            series_mode="single" if intent.get("episode") else "complete").model_dump()
        results = await self.plex_factory().request("POST", "/search", json={
            "query": intent["title"], "indexer": "all", "year": intent.get("year"), "quality": intent.get("quality"),
            "language": intent.get("language"), "season": intent.get("season", 0), "episode": intent.get("episode", 0),
            "rank_preferences": True, "limit": 100})
        if not results["items"]:
            await self.say(message, "La recherche a échoué auprès des indexers. Réessaie plus tard." if results.get("errors")
                           else "Je n’ai trouvé aucun résultat. Essaie un autre titre avec « bot cherche … ».")
            return
        state = {"phase": "confirm", "items": results["items"][:5], "intent": intent, "prefs": prefs,
                 "index": 0, "origin_id": message.id, "request_id": f"conversation:{message.id}:0"}
        await self.propose(message, key, state)

    async def propose(self, message, key, state):
        selected = state["items"][state["index"]]
        title = discord.utils.escape_markdown(selected["title"][:250])
        lines = [f"Je propose d’ajouter **{title}**.", f"Dossier cible : **{discord.utils.escape_markdown(state['prefs']['target_name'])}**."]
        mismatches = [name for name, value in selected.get("preference_matches", {}).items() if value is False]
        labels = {"year": "année", "quality": "qualité", "language": "langue", "season": "saison", "episode": "épisode"}
        if mismatches:
            lines.append("⚠️ Préférences non repérées : " + ", ".join(labels.get(name, name) for name in mismatches) + ". Vérifie le titre proposé.")
        if len(state["items"]) > 1:
            lines.append("Autres choix :\n" + "\n".join(f"**{i + 1}** — {discord.utils.escape_markdown(item['title'][:160])}"
                          for i, item in enumerate(state["items"])))
        lines.append("Réponds **oui** pour ajouter ce résultat, **non** pour annuler, ou un **numéro** pour changer de proposition. Confirmation valable 5 minutes.")
        proposal = await self.say(message, "\n".join(lines))
        state["proposal_message_id"] = proposal.id
        self.save(key, state)
