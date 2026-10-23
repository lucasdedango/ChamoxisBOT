"""Historical Discord interactions, migrated to the central manager.

All Plex operations use the authenticated HTTP bridge.
"""
import os, re, asyncio, logging
from typing import Optional, Tuple, List, Dict, TypedDict
import discord
from discord import app_commands
from discord.ext import commands
from manager.discord.bridge import *
from manager.core.permissions import allowed_user, administrator, ids

logger = logging.getLogger("chamoxisbot.discord")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "369545955252502528"))
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0") or "0")
MAX_SEARCH_RESULTS = 250
SEASON_SELECT_MAX = 20
EPISODE_SELECT_MAX = 30
DISCORD_SELECT_MAX_OPTIONS = 25
INDEXER_IDS = []
INDEXER_LABELS = {}
TORZNAB_RSS_URL = ""
TORZNAB_FORCE_UPLOAD = False
DISABLE_TORRENT_DOWNLOAD = False
PLEX_URL = PLEX_TOKEN = PLEX_MOVIES_SECTION_ID = PLEX_SERIES_SECTION_ID = ""
ImportPrefs = dict
tracked_torrents = {}
imported_torrents = set()
_background_tasks = set()
intents = discord.Intents.default()
intents.message_content = bool(ids("DISCORD_CONVERSATION_CHANNEL_IDS"))
bot = commands.Bot(command_prefix="!", intents=intents)
conversation = None


@bot.event
async def on_message(message):
    if conversation is not None:
        await conversation.handle(message)

def spawn_background(coro, *, name: str) -> asyncio.Task:
    """Start and retain a task; always consume and log its terminal exception."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _finished(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            logger.info("Background task cancelled: %s", done.get_name())
            return
        error = done.exception()
        if error is not None:
            logger.error("Background task failed: %s", done.get_name(), exc_info=(type(error), error, error.__traceback__))

    task.add_done_callback(_finished)
    return task


def parse_torrent_attachment(file: discord.Attachment) -> Dict[str, str]:
    filename = (file.filename or "").lower()
    if not filename.endswith(".torrent"):
        raise ValueError("Fichier invalide: envoie un fichier .torrent.")
    return {"kind": "torrent_url", "normalized_value": file.url}


def shorten(text: str, max_len: int = 90) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def human_size(size_str: str) -> str:
    try:
        n = float(size_str)
    except Exception:
        return "?"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def popularity_badge(item: Dict[str, str]) -> str:
    try:
        seeders = int(item.get("seeders", "") or 0)
    except Exception:
        seeders = 0
    try:
        grabs = int(item.get("grabs", "") or 0)
    except Exception:
        grabs = 0
    # Score simple: seeders comptent plus que les grabs.
    score = seeders * 3 + grabs
    if score >= 200:
        return "🔥"
    if score >= 80:
        return "⭐"
    if score > 0:
        return "👍"
    return "·"


def clamp_embed_field_lines(lines: List[str], limit: int = 1024) -> str:
    """
    Construit un texte <= limit caractères pour un champ embed Discord.
    """
    out: List[str] = []
    used = 0
    for idx, line in enumerate(lines):
        extra = len(line) + (1 if out else 0)
        if used + extra > limit:
            remaining = len(lines) - idx
            suffix = f"… (+{remaining} autres)"
            if out:
                while out and used + 1 + len(suffix) > limit:
                    removed = out.pop()
                    used -= len(removed) + (1 if out else 0)
                if out and used + 1 + len(suffix) <= limit:
                    out.append(suffix)
            elif len(suffix) <= limit:
                out = [suffix]
            break
        out.append(line)
        used += extra
    return "\n".join(out) if out else "Aucun détail"


def extract_quality_options(items: List[Dict[str, str]]) -> List[str]:
    qualities: List[str] = []
    for it in items:
        title = it.get("title", "")
        for q in ("2160p", "1080p", "720p", "480p"):
            if q.lower() in title.lower() and q not in qualities:
                qualities.append(q)
    return qualities


class RssSelect(discord.ui.Select):
    def __init__(self, items: List[Dict[str, str]], category: str, prefs: ImportPrefs | None = None, track: bool = True):
        options = []
        for idx, item in enumerate(items[:DISCORD_SELECT_MAX_OPTIONS], start=1):
            source = item.get("source", "?")
            label = shorten(f"{idx}. [{source}] {item['title']}", 100)
            options.append(discord.SelectOption(label=label, value=str(idx - 1)))
        if not options:
            raise ValueError("Aucun résultat sélectionnable pour le menu Discord.")
        super().__init__(placeholder="Choisis un torrent à ajouter", options=options, min_values=1, max_values=1)
        self.items = items
        self.category = category
        self.prefs = prefs or {}
        self.track = track

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        item = self.items[int(self.values[0])]
        url = item.get("enclosure") or item.get("link")
        if not url:
            await interaction.followup.send("Lien torrent introuvable.", ephemeral=True)
            return
        from manager.discord.commands.plex import offer_download
        prefs = {**self.prefs, "kind": self.category}
        await offer_download(interaction, url, item.get("title", "torrent"), prefs)


class SearchCancelButton(discord.ui.Button):
    def __init__(self, label: str = "🛑 Annuler"):
        super().__init__(label=label, style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        view = self.view
        if isinstance(view, discord.ui.View):
            for child in view.children:
                child.disabled = True
            view.stop()
        await interaction.response.edit_message(content="🛑 Recherche annulée.", view=view)


class RssView(discord.ui.View):
    def __init__(self, items: List[Dict[str, str]], category: str, prefs: ImportPrefs | None = None, track: bool = True, *, timeout: float = 120):
        super().__init__(timeout=timeout)
        if items:
            self.add_item(RssSelect(items, category, prefs=prefs, track=track))
        self.add_item(SearchCancelButton())


async def send_torznab_results(
    interaction: discord.Interaction,
    query: str,
    kind: str,
    prefs: ImportPrefs | None = None,
    track: bool = True,
    quality: str | None = None,
    indexer: str = "all",
    language: str | None = None,
    rank_preferences: bool = False,
    year: int | None = None,
    season: int = 0,
    episode: int = 0,
    selection_policy: bool = False,
    min_seeders: int | None = None,
    prefer_quality: bool = False,
    query_aliases: list[str] | None = None,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
):
    try:
        response = await plex_client().request("POST", "/search", json={
            "query": query, "query_aliases": query_aliases or [], "imdb_id": imdb_id, "tmdb_id": tmdb_id, "media_kind": kind, "indexer": indexer, "quality": quality, "language": language, "limit": MAX_SEARCH_RESULTS,
            "rank_preferences": rank_preferences, "selection_policy": selection_policy,
            "prefer_quality": prefer_quality, "strict_series": selection_policy, "min_seeders": min_seeders,
            "year": year, "season": season, "episode": episode})
        items = response["items"]
    except Exception:
        await interaction.followup.send("Impossible de contacter le module Plex pour la recherche.", ephemeral=True)
        return
    if not items:
        options = response.get("quality_options", [])
        text = "Aucun résultat." if not response["errors"] else "La recherche a échoué ; consulte les logs du module Plex."
        if options:
            text = f"Aucun résultat compatible en {quality}. Qualités disponibles : {', '.join(options)}. Relance `/recherchetorrent` avec la qualité de ton choix."
        await interaction.followup.send(text, ephemeral=True)
        return

    lines = []
    for idx, item in enumerate(items, start=1):
        source = item.get("source", "?")
        pop = popularity_badge(item)
        seeds = item.get("seeders", "?") or "?"
        grabs = item.get("grabs", "?") or "?"
        matches = item.get("preference_matches", {})
        labels = {"year": "année", "quality": "qualité", "language": "langue probable", "season": "saison", "episode": "épisode"}
        matched = [label for name, label in labels.items() if matches.get(name) is True]
        preference_note = " | ✓ " + ", ".join(matched) if matched else ""
        for warning in ("availability_warning", "identity_warning"):
            if item.get(warning):
                preference_note += " | ⚠️ " + item[warning]
        lines.append(
            f"{idx}. {pop} [{source}] {item['title']} — {human_size(item.get('size', ''))} (`{item.get('pub_date','')}`) "
            f"[S:{seeds} G:{grabs}]{preference_note}"
        )

    selected_indexer_name = "Tous" if indexer == "all" else indexer_label(indexer)
    embed = discord.Embed(
        title=f"Prowlarr ({selected_indexer_name}): résultats pour \"{query}\""[:256],
        description="\n".join(lines[:25])[:4096],
    )
    footer = f"Tri: poids décroissant | Popularité: 🔥/⭐/👍 via seeders+grabs | AV1 en dernier recours | Filtre qualité: {quality or 'aucun'}"
    if rank_preferences:
        footer = f"Résultats préférés en premier | Année : {year or 'non précisée'} | Qualité : {quality or 'toutes'} | Langue : {language or 'toutes'} | Alternatives conservées"
    if selection_policy:
        footer = f"Qualité : {quality} | Langue : {language or 'MULTI préféré'} | Seeds positifs préférés, repli avec avertissement | Taille croissante | AV1 en dernier recours"
    if len(lines) > 25:
        footer += f" | {len(lines) - 25} résultat(s) supplémentaire(s) non affiché(s)"
    embed.set_footer(text=footer)

    category = "movies" if kind.lower().strip() == "movies" else "series"
    selectable_items = items[:DISCORD_SELECT_MAX_OPTIONS]
    try:
        view = RssView(selectable_items, category, prefs=prefs, track=track)
    except ValueError as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
        logger.error("Torznab view build failed for query=%s: %s", query, e)
        return
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class TorrentKindView(discord.ui.View):
    def __init__(self, query: str, season: int, episode: int, quality: str | None = None, indexer: str = "all", direct_link: Dict[str, str] | None = None):
        super().__init__(timeout=180)
        self.add_item(SearchCancelButton())
        self.query = query
        self.detected_season = season
        self.detected_episode = episode
        self.quality = quality
        self.indexer = indexer
        self.direct_link = direct_link

    async def _start(self, interaction: discord.Interaction, kind: str):
        await interaction.response.defer(ephemeral=True)
        try:
            sugg = await suggest_target_directories(self.query, kind)
        except Exception:
            await interaction.followup.send("Le module Plex est indisponible. Réessaie après son démarrage.", ephemeral=True)
            return
        await interaction.followup.send(
            "Choisis les options de recherche:",
            ephemeral=True,
            view=TorrentOptionView(
                query=self.query,
                kind=kind,
                suggested_dirs=sugg,
                season=self.detected_season,
                episode=self.detected_episode,
                quality=self.quality,
                indexer=self.indexer,
                direct_link=self.direct_link,
            ),
        )

    @discord.ui.button(label="🎬 Film", style=discord.ButtonStyle.primary)
    async def movie_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._start(interaction, "movies")

    @discord.ui.button(label="📺 Série", style=discord.ButtonStyle.secondary)
    async def series_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._start(interaction, "series")


class TorrentOptionView(discord.ui.View):
    def __init__(
        self,
        query: str,
        kind: str,
        suggested_dirs: List[str],
        season: int = 0,
        episode: int = 0,
        quality: str | None = None,
        indexer: str = "all",
        direct_link: Dict[str, str] | None = None,
    ):
        super().__init__(timeout=300)
        self.query = query
        self.kind = kind
        self.series_mode = "complete"
        self.season = season
        self.episode = episode
        self.target_name: str | None = None
        self.quality = quality
        self.indexer = indexer
        self.direct_link = direct_link

        self.add_item(TorrentManualDirButton())

        if suggested_dirs:
            options = [discord.SelectOption(label=n[:100], value=n) for n in suggested_dirs[:24]]
            options.append(discord.SelectOption(label="Répertoire pas dans la liste", value="__manual__"))
            self.add_item(TorrentDirSelect(options))

        if self.kind == "series":
            mode_opts = [
                discord.SelectOption(label="Série complète / saison", value="complete", default=True),
                discord.SelectOption(label="Épisode unique", value="single"),
            ]
            self.add_item(TorrentSeriesModeSelect(mode_opts))

            season_opts = [discord.SelectOption(label=f"Saison {i}", value=str(i), default=(i == (self.season or 1))) for i in range(1, SEASON_SELECT_MAX + 1)]
            self.add_item(TorrentSeasonSelect(season_opts))

        quality_opts = [
            discord.SelectOption(label="Toutes", value="all"),
            discord.SelectOption(label="2160p", value="2160p"),
            discord.SelectOption(label="1080p", value="1080p"),
            discord.SelectOption(label="720p", value="720p"),
        ]
        self.add_item(TorrentQualitySelect(quality_opts, selected=self.quality or "all"))
        self.add_item(SearchCancelButton())

    @discord.ui.button(label="✅ Confirmer et chercher", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.target_name:
            await safe_send_interaction(
                interaction,
                "⚠️ Choisis un répertoire cible dans la liste ou saisis-le manuellement.",
                ephemeral=True,
            )
            return

        prefs: ImportPrefs = {"kind": self.kind}
        prefs["target_name"] = self.target_name
        if self.kind == "series":
            prefs["series_mode"] = self.series_mode
            if self.season > 0:
                prefs["season"] = self.season
            if self.series_mode == "single" and self.episode > 0:
                prefs["episode"] = self.episode

        summary = [
            f"- Type: {'Série' if self.kind == 'series' else 'Film'}",
            f"- Qualité: {self.quality or 'Toutes'}",
            f"- Dossier cible: {self.target_name}",
        ]
        if self.kind == "series":
            summary.append(f"- Mode série: {'Episode' if self.series_mode == 'single' else 'Complet'}")
            summary.append(f"- Saison: {self.season or 1}")
            if self.series_mode == "single":
                summary.append(f"- Episode: {self.episode or 1}")

        if not await safe_send_interaction(
            interaction,
            ("Configuration validée. Lancement de l'import direct...\n" if self.direct_link else "Configuration validée. Lancement de la recherche...\n") + "\n".join(summary),
            ephemeral=True,
        ):
            return
        if self.direct_link:
            await process_direct_torrent_import(interaction, self.direct_link, self.kind, prefs)
        else:
            await send_torznab_results(
                interaction,
                self.query,
                self.kind,
                prefs=prefs,
                track=True,
                quality=self.quality,
                indexer=self.indexer,
                season=prefs.get("season", 0),
                episode=prefs.get("episode", 0),
            )


async def process_direct_torrent_import(
    interaction: discord.Interaction,
    link_input: Dict[str, str],
    kind: str,
    prefs: ImportPrefs,
) -> None:
    from manager.discord.commands.plex import offer_download
    await offer_download(interaction, link_input["normalized_value"], prefs.get("target_name", "torrent"), prefs)


class TorrentDirSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Nom du répertoire cible", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            selected = self.values[0]
            if selected == "__manual__":
                try:
                    await interaction.response.send_modal(TorrentManualDirModal(parent))
                except discord.NotFound:
                    logger.warning(
                        "Interaction expirée avant ouverture du modal (user=%s id=%s)",
                        getattr(interaction.user, "id", "?"),
                        getattr(interaction, "id", "?"),
                    )
                return
            parent.target_name = selected
        await safe_defer(interaction)


class TorrentManualDirModal(discord.ui.Modal, title="Répertoire cible"):
    target_input = discord.ui.TextInput(
        label="Nom du répertoire cible",
        placeholder="Ex: Andor (2022)",
        max_length=100,
    )

    def __init__(self, parent_view: TorrentOptionView):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.target_input.value).strip()
        if not value:
            await interaction.response.send_message("⚠️ Le nom du répertoire ne peut pas être vide.", ephemeral=True)
            return
        self.parent_view.target_name = value
        await interaction.response.send_message(f"✅ Répertoire cible défini: `{value}`", ephemeral=True)


class TorrentManualDirButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✍️ Saisir un répertoire cible", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if not isinstance(parent, TorrentOptionView):
            await safe_defer(interaction)
            return
        try:
            await interaction.response.send_modal(TorrentManualDirModal(parent))
        except discord.NotFound:
            logger.warning("Interaction expirée avant ouverture du modal (user=%s id=%s)", getattr(interaction.user, "id", "?"), getattr(interaction, "id", "?"))


class TorrentSeriesModeSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Mode série", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            parent.series_mode = self.values[0]
        await safe_defer(interaction)


class TorrentSeasonSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Saison", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            parent.season = int(self.values[0])
        await safe_defer(interaction)


class TorrentQualitySelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], selected: str):
        for o in options:
            o.default = (o.value == selected)
        super().__init__(placeholder="Qualité", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            parent.quality = None if self.values[0] == "all" else self.values[0]
        await safe_defer(interaction)


@bot.event
async def setup_hook():
    # Manager and Discord remain available even if the Plex module is down.
    global INDEXER_IDS, INDEXER_LABELS
    try:
        remote = await plex_client().request("GET", "/config")
        INDEXER_IDS = remote["indexer_ids"]
        INDEXER_LABELS = remote["indexer_labels"]
    except Exception:
        logger.warning("Plex module unavailable during Discord startup")
    # Synchroniser aussi le scope global supprime les anciennes commandes qui
    # n'existent plus localement (notamment l'ancien /import).
    global_synced = await bot.tree.sync()
    synced = []
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
    logger.info(
        "Synced commands globally=%s and guild=%s (%s): %s",
        len(global_synced), len(synced), GUILD_ID, [c.name for c in synced],
    )


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        logger.warning("Interaction expirée avant defer (user=%s id=%s)", getattr(interaction.user, "id", "?"), getattr(interaction, "id", "?"))
    except discord.HTTPException:
        logger.exception("Echec defer interaction (user=%s id=%s)", getattr(interaction.user, "id", "?"), getattr(interaction, "id", "?"))
    return False


async def safe_send_interaction(interaction: discord.Interaction, *args, **kwargs) -> bool:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(*args, **kwargs)
        else:
            await interaction.response.send_message(*args, **kwargs)
        return True
    except discord.NotFound:
        logger.warning("Interaction expirée avant réponse (user=%s id=%s)", getattr(interaction.user, "id", "?"), getattr(interaction, "id", "?"))
    except discord.HTTPException:
        logger.exception("Echec envoi réponse interaction (user=%s id=%s)", getattr(interaction.user, "id", "?"), getattr(interaction, "id", "?"))
    return False


@bot.event
async def on_ready():
    logger.info("Connecté en tant que %s", bot.user)


@bot.event
async def on_error(event: str, *args, **kwargs):
    logger.exception("Unhandled Discord event error on %s", event)


async def check_command_access(interaction: discord.Interaction) -> bool:
    command_name = getattr(getattr(interaction, "command", None), "qualified_name", "")
    if command_name == "aide" or allowed_user(interaction.user.id):
        return True
    await interaction.response.send_message("Utilisateur non autorisé. Demande l’accès à un administrateur ; /aide reste accessible.", ephemeral=True)
    return False


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, discord.NotFound) and original.code in {10062, 10015}:
        logger.warning("Discord interaction expired: command=%s interaction=%s code=%s; retry the command", getattr(getattr(interaction, "command", None), "qualified_name", "unknown"), interaction.id, original.code)
        return  # Retrying an expired interaction would only produce a second error.
    logger.exception(
        "App command error: command=%s user=%s(%s) guild=%s",
        getattr(getattr(interaction, "command", None), "qualified_name", "unknown"),
        getattr(interaction.user, "name", "unknown"),
        getattr(interaction.user, "id", "unknown"),
        getattr(getattr(interaction, "guild", None), "id", "DM"),
    )
    try:
        if isinstance(error, app_commands.CommandNotFound):
            msg = "❌ Commande inconnue. Utilise `/aide` pour voir les commandes disponibles."
        else:
            msg = "❌ La commande n’a pas pu être terminée. Réessaie ou consulte /aide ; les détails techniques sont dans les logs."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        logger.exception("Failed to send app command error message to Discord")


async def status(interaction: discord.Interaction):
    logger.info("/admin torrents called by %s (%s)", interaction.user, interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    try:
        items = await qbit.list_torrents(limit=10)
        if not items:
            await interaction.followup.send("Aucun torrent.", ephemeral=True)
            return

        lines = []
        for t in items:
            name = t.get("name", "???")
            progress = int((t.get("progress", 0.0) * 100))
            state = t.get("state", "???")
            cat = t.get("category", "") or "-"
            lines.append(f"- {name} — {progress}% — {state} — cat:{cat}")

        await interaction.followup.send("**Derniers torrents :**\n" + "\n".join(lines), ephemeral=True)
    except Exception:
        logger.exception("/admin torrents failed")
        await interaction.followup.send("Impossible de consulter les torrents actuellement.", ephemeral=True)


@bot.tree.command(name="recherchetorrent", description="Recherche privée de film ou série avec choix de l’œuvre et confirmation")
@app_commands.describe(query="Titre ou demande complète, ex. Les Anneaux de pouvoir S01 en 1080p")
async def recherchetorrent(interaction: discord.Interaction, query: str = ""):
    from manager.discord.commands.plex import demande
    await demande(interaction, query)


@bot.tree.command(name="ajoutertorrent", description="Ajoute un lien magnet/.torrent puis lance le formulaire d'import.")
@app_commands.describe(
    link="Lien magnet ou URL .torrent",
    file="Fichier .torrent en pièce jointe",
)
async def addtorrent(interaction: discord.Interaction, link: str | None = None, file: discord.Attachment | None = None):
    logger.info("/ajoutertorrent called by %s (%s)", interaction.user, interaction.user.id)
    if bool(link) == bool(file):
        await interaction.response.send_message("❌ Fournis soit `link`, soit `file` (.torrent), mais pas les deux.", ephemeral=True)
        return
    try:
        parsed = parse_torrent_input(link) if link else parse_torrent_attachment(file)  # type: ignore[arg-type]
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    content = (
        f"✅ Lien accepté (`{parsed['kind']}`).\n"
        "Choisis Film / Série puis renseigne les options (dossier cible, saison/épisode), "
        "et confirme pour lancer l'import direct."
    )
    await interaction.response.send_message(
        content,
        view=TorrentKindView(link or (file.filename if file else "upload.torrent"), 0, 0, quality=None, indexer="all", direct_link=parsed),
        ephemeral=True,
    )


# Older import helpers remain internal; publish only the reviewed command surface.
from manager.discord.commands.plex import operations
from manager.discord.commands import admin, ai, help as user_help
bot.tree.add_command(app_commands.Command(name="telechargements", description="Consulter tes demandes et imports en privé", callback=operations))
bot.tree.interaction_check = check_command_access
user_help.register(bot.tree)
ai.register(bot.tree)
admin.register(bot.tree, status)
