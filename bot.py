import os
import re
import asyncio
import shutil
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ----------------- CONFIG -----------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

QBIT_URL = os.getenv("QBIT_URL", "http://127.0.0.1:8080").rstrip("/")
QBIT_USER = os.getenv("QBIT_USER", "")
QBIT_PASS = os.getenv("QBIT_PASS", "")

# Plex (optionnel)
PLEX_URL = os.getenv("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
PLEX_MOVIES_SECTION_ID = os.getenv("PLEX_MOVIES_SECTION_ID", "")
PLEX_SERIES_SECTION_ID = os.getenv("PLEX_SERIES_SECTION_ID", "")

# Dossiers Plex chez toi
PLEX_ROOT = Path(r"D:\plexmediaserver")
PLEX_MOVIES = PLEX_ROOT / "movies"
PLEX_SERIES = PLEX_ROOT / "series"

# Ton serveur Discord (sync instant)
GUILD_ID = 369545955252502528

# Extensions vidéo
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts"}

# Patterns séries
SERIES_PATTERNS = [
    re.compile(r"\bS(?P<s>\d{1,2})E(?P<e>\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(?P<s>\d{1,2})x(?P<e>\d{1,3})\b", re.IGNORECASE),
]
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

JUNK_TOKENS = {
    "1080p", "720p", "2160p", "4k", "uhd", "hdr", "hdr10", "dv", "dolby", "vision",
    "web", "webrip", "web-dl", "bluray", "bdrip", "brrip", "hdtv", "dvdrip", "remux",
    "x264", "x265", "h264", "h265", "hevc", "av1",
    "aac", "ac3", "dts", "truehd", "atmos",
    "multi", "french", "vostfr", "vost", "vf", "vff", "vfi",
    "proper", "repack", "extended", "uncut",
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- QBITTORRENT CLIENT -----------------
class QbitClient:
    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url
        self.user = user
        self.password = password
        self.session: aiohttp.ClientSession | None = None
        self.logged_in = False

    async def start(self):
        # CookieJar(unsafe=True) indispensable sur 127.0.0.1 pour garder le SID -> sinon 403
        if self.session is None or self.session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(cookie_jar=jar, timeout=timeout)
        self.logged_in = False

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def login(self):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        url = f"{self.base_url}/api/v2/auth/login"
        data = {"username": self.user, "password": self.password}
        async with self.session.post(url, data=data) as r:
            text = await r.text()
            if r.status != 200 or text.strip() != "Ok.":
                raise RuntimeError(f"Login qBittorrent échoué: HTTP {r.status} / {text}")
        self.logged_in = True

    async def ensure_login(self):
        if not self.logged_in:
            await self.login()

    async def _get_json(self, path: str, params: dict | None = None):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        await self.ensure_login()

        url = f"{self.base_url}{path}"
        async with self.session.get(url, params=params) as r:
            if r.status == 403:
                self.logged_in = False
                await self.ensure_login()
                async with self.session.get(url, params=params) as r2:
                    if r2.status != 200:
                        raise RuntimeError(f"HTTP {r2.status} / {await r2.text()}")
                    return await r2.json()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} / {await r.text()}")
            return await r.json()

    async def _post_text(self, path: str, data: dict):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        await self.ensure_login()

        url = f"{self.base_url}{path}"
        async with self.session.post(url, data=data) as r:
            if r.status == 403:
                self.logged_in = False
                await self.ensure_login()
                async with self.session.post(url, data=data) as r2:
                    if r2.status != 200:
                        raise RuntimeError(f"HTTP {r2.status} / {await r2.text()}")
                    return await r2.text()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} / {await r.text()}")
            return await r.text()

    async def list_torrents(self, limit: int = 10):
        items = await self._get_json("/api/v2/torrents/info", params={"sort": "added_on", "reverse": "true"})
        return items[:limit]

    async def list_completed(self):
        items = await self._get_json("/api/v2/torrents/info", params={"sort": "added_on", "reverse": "true"})
        out = []
        for t in items:
            if float(t.get("progress", 0.0)) >= 1.0:
                out.append(t)
        return out

    async def add_magnet(self, magnet: str, category: str | None = None):
        data = {"urls": magnet}
        if category:
            data["category"] = category
        await self._post_text("/api/v2/torrents/add", data=data)

    async def get_torrent_by_hash(self, info_hash: str) -> Optional[dict]:
        items = await self._get_json("/api/v2/torrents/info", params={"hashes": info_hash})
        if not items:
            return None
        return items[0]

qbit = QbitClient(QBIT_URL, QBIT_USER, QBIT_PASS)

# ----------------- PLEX REFRESH -----------------
async def plex_refresh(section_id: str) -> Tuple[bool, str]:
    if not (PLEX_URL and PLEX_TOKEN and section_id):
        return False, "Plex non configuré"
    url = f"{PLEX_URL}/library/sections/{section_id}/refresh"
    headers = {"X-Plex-Token": PLEX_TOKEN}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, headers=headers) as r:
                if r.status in (200, 201):
                    return True, "OK"
                return False, f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)

# ----------------- HELPERS (NOMS / FICHIERS / MOVE) -----------------
def normalize_spaces(s: str) -> str:
    s = s.replace(".", " ").replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def strip_brackets(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^\)]*\)", " ", s)
    return normalize_spaces(s)

def remove_junk_tokens(s: str) -> str:
    parts = normalize_spaces(s).split(" ")
    cleaned = []
    for p in parts:
        if p.lower() in JUNK_TOKENS:
            continue
        cleaned.append(p)
    return normalize_spaces(" ".join(cleaned))

def extract_season_episode(text: str) -> Optional[Tuple[int, int]]:
    for pat in SERIES_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group("s")), int(m.group("e"))
    return None

def looks_like_series_name(name: str) -> bool:
    return extract_season_episode(name) is not None

def all_video_files(root: Path) -> List[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_EXTS else []
    out: List[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            out.append(p)
    return out

def pick_biggest_video(files: List[Path]) -> Optional[Path]:
    if not files:
        return None
    return max(files, key=lambda x: x.stat().st_size)

def unique_path(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem = dst.stem
    suf = dst.suffix
    parent = dst.parent
    i = 1
    while True:
        cand = parent / f"{stem}-{i}{suf}"
        if not cand.exists():
            return cand
        i += 1

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def move_file(src: Path, dst: Path, logs: List[str] | None = None) -> Path:
    ensure_dir(dst.parent)
    dst2 = unique_path(dst)
    shutil.move(str(src), str(dst2))
    if logs is not None:
        logs.append(f"{src} → {dst2}")
    return dst2

def cleanup_empty_dirs(root: Path):
    if not root.exists() or not root.is_dir():
        return
    for p in sorted(root.rglob("*"), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass

def pick_content_path(t: dict) -> Optional[Path]:
    cp = t.get("content_path")
    if cp:
        p = Path(cp)
        if p.exists():
            return p
    sp = t.get("save_path")
    name = t.get("name")
    if sp and name:
        p = Path(sp) / name
        if p.exists():
            return p
    return None

def guess_show_title_from_torrent(torrent_name: str) -> str:
    base = strip_brackets(torrent_name)
    # Coupe avant SxxExx si présent
    for pat in SERIES_PATTERNS:
        m = pat.search(base)
        if m:
            base = base[:m.start()]
            break
    base = remove_junk_tokens(base)
    base = base.strip(" -._")
    return base or "Unknown Show"

def build_movie_title_and_year(stem: str) -> Tuple[str, Optional[str]]:
    base = strip_brackets(stem)
    year = None
    m = YEAR_RE.search(base)
    if m:
        year = m.group(1)
        base = base[:m.start()]
    base = remove_junk_tokens(base)
    base = base.strip(" -._")
    title = base or "Unknown Movie"
    return title, year

def parse_info_hash_from_magnet(magnet: str) -> Optional[str]:
    """
    Extrait l'info hash (btih) depuis un lien magnet.
    """
    m = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})", magnet)
    if not m:
        return None
    return m.group(1).lower()

# ----------------- IMPORT LOGIC -----------------
def import_series(torrent_name: str, content_root: Path, move_logs: List[str] | None = None) -> Tuple[str, int]:
    """
    Déplace TOUS les fichiers vidéo de la série:
    r"series\Show\Season XX\Show - SXXEYY.ext"
    Retourne (show_title, nb_fichiers_deplaces)
    """
    show = guess_show_title_from_torrent(torrent_name)
    files = all_video_files(content_root)
    if not files:
        raise RuntimeError("aucune vidéo trouvée")

    known: List[Tuple[int, int, Path]] = []
    unknown: List[Path] = []

    for f in files:
        se = extract_season_episode(f.stem)
        if se:
            known.append((se[0], se[1], f))
        else:
            unknown.append(f)

    known.sort(key=lambda x: (x[0], x[1], x[2].name))
    unknown.sort(key=lambda x: x.name)

    moved = 0
    season_counts: Dict[int, int] = {}

    # 1) fichiers avec S/E
    for season, ep, f in known:
        season_counts[season] = season_counts.get(season, 0) + 1
        season_dir = PLEX_SERIES / show / f"Season {season:02d}"
        new_filename = f"{show} - S{season:02d}E{ep:02d}{f.suffix.lower()}"
        move_file(f, season_dir / new_filename, move_logs)
        moved += 1

    # 2) fichiers sans S/E -> on devine la saison
    if season_counts:
        guessed_season = sorted(season_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    else:
        guessed_season = 1

    max_ep = 0
    for s, e, _ in known:
        if s == guessed_season and e > max_ep:
            max_ep = e
    ep_counter = max_ep + 1 if max_ep > 0 else 1

    for f in unknown:
        season_dir = PLEX_SERIES / show / f"Season {guessed_season:02d}"
        new_filename = f"{show} - S{guessed_season:02d}E{ep_counter:02d}{f.suffix.lower()}"
        move_file(f, season_dir / new_filename, move_logs)
        moved += 1
        ep_counter += 1

    # nettoyage
    if content_root.is_dir():
        cleanup_empty_dirs(content_root)

    return show, moved

def import_movie(torrent_name: str, content_root: Path, move_logs: List[str] | None = None) -> Tuple[str, Path]:
    """
    Déplace le plus gros fichier vidéo en:
    r"movies\Title (Year)\Title (Year).ext"
    Retourne (display_name, new_path)
    """
    files = all_video_files(content_root)
    if not files:
        raise RuntimeError("aucune vidéo trouvée")

    video = pick_biggest_video(files)
    if not video:
        raise RuntimeError("aucune vidéo trouvée")

    title, year = build_movie_title_and_year(video.stem)
    display = f"{title} ({year})" if year else title

    movie_dir = PLEX_MOVIES / display
    new_filename = f"{display}{video.suffix.lower()}"
    new_path = move_file(video, movie_dir / new_filename, move_logs)

    if content_root.is_dir():
        cleanup_empty_dirs(content_root)

    return display, new_path

class ConfirmView(discord.ui.View):
    def __init__(self, *, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.value: Optional[bool] = None

    @discord.ui.button(label="Oui", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        self.value = True
        self.stop()
        await interaction.response.edit_message(content="Confirmation reçue, import en cours…", view=None)

    @discord.ui.button(label="Non", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="Import annulé sur demande.", view=None)

async def track_download_progress(interaction: discord.Interaction, info_hash: str, label: str):
    """
    Suit le téléchargement et met à jour un message ephemeral.
    """
    message = await interaction.followup.send(f"📥 Suivi de `{label}`…", ephemeral=True)
    max_iterations = 120  # ~10 minutes avec sleep(5)
    for _ in range(max_iterations):
        try:
            info = await qbit.get_torrent_by_hash(info_hash)
        except Exception as e:
            await message.edit(content=f"⚠️ Suivi interrompu: {e}")
            return

        if not info:
            await message.edit(content="⚠️ Torrent introuvable pour le suivi.")
            return

        progress = int(info.get("progress", 0.0) * 100)
        state = info.get("state", "???")
        speed = int(info.get("dlspeed", 0))
        eta = info.get("eta", None)

        eta_str = "?" if eta is None or eta < 0 else f"{eta // 60} min"
        content = f"📥 `{label}` — {progress}% — état: {state} — ↓ {speed // 1024} KiB/s — ETA {eta_str}"
        await message.edit(content=content)

        if progress >= 100 or state.lower().startswith("stalledup") or state.lower().startswith("upload"):
            await message.edit(content=f"✅ `{label}` terminé ({progress}%, état {state}).")
            return
        await asyncio.sleep(5)

    await message.edit(content="⚠️ Suivi arrêté après délai — dernier état affiché.")

# ----------------- DISCORD EVENTS -----------------
@bot.event
async def setup_hook():
    await qbit.start()
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    print(f"✅ Synced {len(synced)} command(s) to guild {GUILD_ID}: {[c.name for c in synced]}")

@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user}")

# ----------------- COMMANDS -----------------
@bot.tree.command(name="status", description="Liste les derniers torrents qBittorrent.")
async def status(interaction: discord.Interaction):
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
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur qBittorrent : {e}", ephemeral=True)

@bot.tree.command(name="addmagnet", description="Ajoute un magnet à qBittorrent (usage légal).")
@app_commands.describe(
    magnet="Lien magnet",
    kind="movies ou series",
    track="Suivre automatiquement la progression du téléchargement",
)
async def addmagnet(interaction: discord.Interaction, magnet: str, kind: str = "movies", track: bool = True):
    await interaction.response.defer(ephemeral=True)
    category = "movies" if kind.lower().strip() == "movies" else "series"
    info_hash = parse_info_hash_from_magnet(magnet)
    try:
        await qbit.add_magnet(magnet, category=category)
        await interaction.followup.send(f"✅ Magnet ajouté (catégorie: {category}).", ephemeral=True)
        if track:
            if info_hash:
                label = f"{category} ({info_hash[:8]})"
                asyncio.create_task(track_download_progress(interaction, info_hash, label))
            else:
                await interaction.followup.send("⚠️ Suivi automatique indisponible (hash introuvable dans le magnet).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur qBittorrent : {e}", ephemeral=True)

@bot.tree.command(name="import", description="Smart import Plex: trie série/film + renomme + refresh.")
@app_commands.describe(max_items="Nombre max de torrents à importer (défaut 5)")
async def import_cmd(interaction: discord.Interaction, max_items: int = 5):
    await interaction.response.defer(ephemeral=True)

    moved_items = 0
    moved_files = 0
    did_movies = False
    did_series = False
    results: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []

    try:
        completed = await qbit.list_completed()
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur qBittorrent : {e}", ephemeral=True)
        return

    if not completed:
        await interaction.followup.send("Aucun torrent terminé à importer.", ephemeral=True)
        return

    planned = []
    for t in completed[:max_items]:
        torrent_name = t.get("name", "???")
        category = (t.get("category") or "").lower().strip()
        content_root = pick_content_path(t)
        if not content_root:
            skipped.append(f"{torrent_name} (chemin introuvable)")
            continue

        is_series = (category == "series") or looks_like_series_name(torrent_name)
        is_forced_movie = (category == "movies")
        planned.append({
            "torrent": t,
            "name": torrent_name,
            "content_root": content_root,
            "is_series": is_series and not is_forced_movie,
        })

    if not planned and not skipped:
        await interaction.followup.send("Aucun torrent prêt à être importé.", ephemeral=True)
        return

    plan_lines = [f"- `{p['name']}` → {'série' if p['is_series'] else 'film'} (source: `{p['content_root']}`)" for p in planned[:10]]
    if len(planned) > 10:
        plan_lines.append(f"+ {len(planned) - 10} autres…")

    confirm_embed = discord.Embed(
        title="Confirmer l'import ?",
        description="Le bot va déplacer et renommer les fichiers comme indiqué ci-dessous.",
    )
    if plan_lines:
        confirm_embed.add_field(name="Plan", value="\n".join(plan_lines), inline=False)
    if skipped:
        confirm_embed.add_field(name="⚠️ Ignorés", value="\n".join(f"- {x}" for x in skipped[:10]), inline=False)

    view = ConfirmView(timeout=120)
    prompt_msg = await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)
    await view.wait()
    await prompt_msg.edit(view=None)

    if view.value is not True:
        await interaction.followup.send("Import annulé.", ephemeral=True)
        return

    move_logs: List[str] = []
    for p in planned:
        torrent_name = p["name"]
        content_root = p["content_root"]
        try:
            if p["is_series"]:
                show, n = import_series(torrent_name, content_root, move_logs)
                did_series = True
                moved_items += 1
                moved_files += n
                target_path = PLEX_SERIES / show
                results.append(f"📺 Série: `{torrent_name}` → {n} fichier(s) dans `{target_path}`")
            else:
                display, new_path = import_movie(torrent_name, content_root, move_logs)
                did_movies = True
                moved_items += 1
                moved_files += 1
                results.append(f"🎬 Film: `{torrent_name}` → `{new_path}`")

        except Exception as e:
            errors.append(f"{torrent_name} ({e})")

    # Refresh Plex (si configuré)
    refresh_lines = []
    if moved_items > 0:
        if did_movies and PLEX_MOVIES_SECTION_ID:
            ok, msg = await plex_refresh(PLEX_MOVIES_SECTION_ID)
            refresh_lines.append(f"🎬 Movies refresh: {'OK' if ok else 'KO'} ({msg})")
        if did_series and PLEX_SERIES_SECTION_ID:
            ok, msg = await plex_refresh(PLEX_SERIES_SECTION_ID)
            refresh_lines.append(f"📺 Series refresh: {'OK' if ok else 'KO'} ({msg})")
        if not refresh_lines and (PLEX_URL and PLEX_TOKEN):
            refresh_lines.append("ℹ️ Plex configuré mais section id manquant.")
        if not (PLEX_URL and PLEX_TOKEN):
            refresh_lines.append("ℹ️ Plex refresh non configuré (scan auto Plex devrait suffire).")

    # Embed stylé
    embed = discord.Embed(
        title="📦 Smart Import Plex",
        description=f"Torrents traités: **{min(max_items, len(completed))}** | Importés: **{moved_items}** | Fichiers déplacés: **{moved_files}**",
    )

    if results:
        embed.add_field(name="✅ Résultats", value="\n".join(results[:12]), inline=False)
        if len(results) > 12:
            embed.add_field(name="…", value=f"+ {len(results) - 12} autres", inline=False)

    if 'move_logs' in locals() and move_logs:
        embed.add_field(name="📂 Copie des fichiers", value="\n".join(move_logs[:12]), inline=False)

    if skipped:
        embed.add_field(name="⚠️ Ignorés", value="\n".join(f"- {x}" for x in skipped[:10]), inline=False)

    if errors:
        embed.add_field(name="❌ Erreurs", value="\n".join(f"- {x}" for x in errors[:10]), inline=False)

    if refresh_lines:
        embed.add_field(name="🔄 Plex", value="\n".join(refresh_lines), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# ----------------- MAIN -----------------
async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN manquant dans .env")
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await qbit.close()

if __name__ == "__main__":
    asyncio.run(main())
