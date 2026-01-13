import os
import re
import asyncio
import shutil
import base64
import logging
from pathlib import Path
from typing import Optional, Tuple, List, Dict, TypedDict
import xml.etree.ElementTree as ET
import urllib.parse

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

# Jackett (RSS)
JACKETT_URL = os.getenv("JACKETT_URL", "http://127.0.0.1:9117").rstrip("/")
JACKETT_API_KEY = os.getenv("JACKETT_API_KEY", "")
JACKETT_RSS_URL = os.getenv(
    "JACKETT_RSS_URL",
    f"{JACKETT_URL}/api/v2.0/indexers/ygege/results/torznab/api?apikey={JACKETT_API_KEY}&limit=20",
)
JACKETT_USER_AGENT = os.getenv(
    "JACKETT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
)
JACKETT_USER = os.getenv("JACKETT_USER", "")
JACKETT_PASSWORD = os.getenv("JACKETT_PASSWORD", "")
JACKETT_COOKIE_NAME = os.getenv("JACKETT_COOKIE_NAME", "")
JACKETT_COOKIE_VALUE = os.getenv("JACKETT_COOKIE_VALUE", "")
JACKETT_INDEXER = "ygege"
JACKETT_COOLDOWN_SECONDS = 30

LOG_FILE = os.getenv("BOT_LOG_FILE", "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger("chamoxisbot")

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


class TrackedTorrent(TypedDict):
    user_id: int
    user_label: str


tracked_torrents: Dict[str, TrackedTorrent] = {}
imported_torrents: set[str] = set()
pending_import_prompts: set[str] = set()

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

    async def add_torrent_file(self, torrent_bytes: bytes, filename: str = "download.torrent", category: str | None = None):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        await self.ensure_login()

        async def _post_once():
            form = aiohttp.FormData()
            form.add_field(
                "torrents",
                torrent_bytes,
                filename=filename,
                content_type="application/x-bittorrent",
            )
            if category:
                form.add_field("category", category)
            url = f"{self.base_url}/api/v2/torrents/add"
            async with self.session.post(url, data=form) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} / {await r.text()}")

        try:
            await _post_once()
        except RuntimeError as e:
            # Tentative de relogin si le SID a expiré
            if "HTTP 403" in str(e):
                self.logged_in = False
                await self.ensure_login()
                await _post_once()
            else:
                raise

    async def get_torrent_by_hash(self, info_hash: str) -> Optional[dict]:
        items = await self._get_json("/api/v2/torrents/info", params={"hashes": info_hash})
        if not items:
            return None
        return items[0]

    async def set_location(self, info_hash: str, location: Path):
        data = {"hashes": info_hash, "location": str(location)}
        await self._post_text("/api/v2/torrents/setLocation", data=data)

    async def rename_file(self, info_hash: str, old: Path, new: Path):
        data = {"hash": info_hash, "oldPath": old.as_posix(), "newPath": new.as_posix()}
        await self._post_text("/api/v2/torrents/renameFile", data=data)

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


def unique_rel_path(base_dir: Path, rel: Path) -> Path:
    """
    Retourne un chemin relatif non existant sous base_dir en ajoutant des suffixes si besoin.
    """
    candidate = rel
    stem = rel.stem
    suffix = rel.suffix
    parent = rel.parent
    i = 1
    while (base_dir / candidate).exists():
        candidate = parent / f"{stem}-{i}{suffix}"
        i += 1
    return candidate

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def move_file(src: Path, dst: Path, logs: List[str] | None = None) -> Path:
    """
    Ancienne fonction de déplacement local (conservée pour compatibilité).
    Les imports utilisent désormais l'API qBittorrent pour déplacer/renommer.
    """
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


def find_torrent_by_name_sync(items: List[dict], name: str) -> Optional[dict]:
    for t in items:
        if t.get("name") == name:
            return t
    return None


def remember_tracked_torrent(info_hash: str, user: discord.abc.User):
    """
    Enregistre un torrent comme suivi pour un utilisateur précis.
    """
    tracked_torrents[info_hash] = {
        "user_id": user.id,
        "user_label": user.display_name or user.name,
    }


def check_user_ownership(info_hash: str, user_id: int) -> Tuple[bool, str]:
    if info_hash in imported_torrents:
        return False, "déjà importé"
    tracked = tracked_torrents.get(info_hash)
    if not tracked:
        return False, "non suivi (/cleartorrents ou historique)"
    if tracked["user_id"] != user_id:
        return False, f"réservé à {tracked['user_label']}"
    return True, ""


def parse_rss_feed(xml_text: str, limit: int = 10) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return items
    for item in channel.findall("item"):
        title = (item.findtext("title") or "???").strip()
        link = (item.findtext("link") or "").strip()
        enclosure = item.find("enclosure")
        enclosure_url = enclosure.attrib.get("url") if enclosure is not None else ""
        pub_date = (item.findtext("pubDate") or "").strip()
        items.append(
            {
                "title": title,
                "link": link,
                "enclosure": enclosure_url,
                "pub_date": pub_date,
            }
        )
        if len(items) >= limit:
            break
    return items


def shorten(text: str, max_len: int = 90) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def build_jackett_search_url(query: str, limit: int = 20) -> str:
    q = urllib.parse.quote_plus(query)
    idx_path = urllib.parse.quote(JACKETT_INDEXER, safe="")
    base = f"{JACKETT_URL}/api/v2.0/indexers/{idx_path}/results/torznab/api"
    return f"{base}?apikey={JACKETT_API_KEY}&t=search&q={q}&limit={limit}"


def _jackett_auth_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if JACKETT_USER and JACKETT_PASSWORD:
        token = base64.b64encode(f"{JACKETT_USER}:{JACKETT_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    if JACKETT_USER_AGENT:
        headers["User-Agent"] = JACKETT_USER_AGENT
    return headers


async def jackett_request(url: str, *, expect_json: bool = False) -> str | list | dict:
    """
    Fait une requête GET Jackett, tente d'abord sans auth, puis avec Basic Auth / cookie si configuré,
    et gère explicitement les 302/401/403.
    """
    timeout = aiohttp.ClientTimeout(total=25)
    base_headers = {"User-Agent": JACKETT_USER_AGENT} if JACKETT_USER_AGENT else {}

    async def _attempt(use_auth: bool):
        headers = dict(base_headers)
        jar = aiohttp.CookieJar(unsafe=True)
        if use_auth:
            headers.update(_jackett_auth_headers())
            if JACKETT_COOKIE_NAME and JACKETT_COOKIE_VALUE:
                jar.update_cookies({JACKETT_COOKIE_NAME: JACKETT_COOKIE_VALUE})
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookie_jar=jar) as session:
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status == 200:
                    return await (resp.json() if expect_json else resp.text())
                # Redirections ou refus : on laisse une seconde chance si pas encore en auth
                if resp.status in (301, 302, 303, 307, 308) and not use_auth:
                    return None
                if resp.status in (401, 403) and not use_auth:
                    return None
                text = await resp.text()
                loc = resp.headers.get("Location", "")
                extra = f" (redir {loc[:80]})" if loc else ""
                raise RuntimeError(f"HTTP {resp.status}{extra} / {text[:120]}")

    # Premier essai sans auth explicite
    logger.info("Jackett GET %s (auth=none)", url)
    result = await _attempt(False)
    if result is not None:
        return result

    # Second essai avec Basic Auth / cookie si dispo
    if not (JACKETT_USER or (JACKETT_COOKIE_NAME and JACKETT_COOKIE_VALUE)):
        logger.warning("Jackett auth required but no credentials/cookies configured.")
        raise RuntimeError("Accès Jackett refusé (auth requise ?)")
    logger.info("Jackett GET %s (auth=basic/cookie)", url)
    result = await _attempt(True)
    if result is not None:
        return result
    logger.error("Jackett access refused for %s", url)
    raise RuntimeError("Accès Jackett refusé malgré authentification.")


async def fetch_rss(url: str, *, user_agent: str = JACKETT_USER_AGENT) -> str:
    """
    Récupère un flux RSS générique (utilisé pour Jackett).
    """
    # user_agent param conservé pour compat mais on passe par jackett_request pour gérer l'auth/cookies.
    return str(await jackett_request(url, expect_json=False))


async def import_torrent_entry(torrent: dict, move_logs: List[str]) -> Tuple[str, bool, bool, int, str]:
    """
    Retourne (message, did_series, did_movies, moved_files, info_hash)
    """
    torrent_name = torrent.get("name", "???")
    category = (torrent.get("category") or "").lower().strip()
    content_root = pick_content_path(torrent)
    if not content_root:
        raise RuntimeError("chemin introuvable")

    info_hash = torrent.get("hash", "")
    is_series = (category == "series") or looks_like_series_name(torrent_name)
    is_forced_movie = (category == "movies")

    if is_series and not is_forced_movie:
        show, n = await import_series(torrent_name, info_hash, content_root, move_logs)
        target_path = PLEX_SERIES / show
        msg = f"📺 Série: `{torrent_name}` → {n} fichier(s) dans `{target_path}`"
        return msg, True, False, n, info_hash

    display, new_path = await import_movie(torrent_name, info_hash, content_root, move_logs)
    msg = f"🎬 Film: `{torrent_name}` → `{new_path}`"
    return msg, False, True, 1, info_hash

# ----------------- IMPORT LOGIC -----------------
async def import_series(torrent_name: str, info_hash: str, content_root: Path, move_logs: List[str] | None = None) -> Tuple[str, int]:
    r"""
    Déplace TOUS les fichiers vidéo de la série:
    series\Show\Season XX\Show - SXXEYY.ext
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

    target_root = PLEX_SERIES / show
    ensure_dir(target_root)
    await qbit.set_location(info_hash, target_root)

    moved = 0
    season_counts: Dict[int, int] = {}

    # 1) fichiers avec S/E
    for season, ep, f in known:
        season_counts[season] = season_counts.get(season, 0) + 1
        season_dir = Path(f"Season {season:02d}")
        new_filename = f"{show} - S{season:02d}E{ep:02d}{f.suffix.lower()}"
        new_rel = unique_rel_path(target_root, season_dir / new_filename)
        ensure_dir(target_root / new_rel.parent)
        rel_old = f.relative_to(content_root) if content_root.is_dir() else Path(f.name)
        await qbit.rename_file(info_hash, rel_old, new_rel)
        moved += 1
        if move_logs is not None:
            move_logs.append(f"{target_root / rel_old} → {target_root / new_rel}")

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
        season_dir = Path(f"Season {guessed_season:02d}")
        new_filename = f"{show} - S{guessed_season:02d}E{ep_counter:02d}{f.suffix.lower()}"
        new_rel = unique_rel_path(target_root, season_dir / new_filename)
        ensure_dir(target_root / new_rel.parent)
        rel_old = f.relative_to(content_root) if content_root.is_dir() else Path(f.name)
        await qbit.rename_file(info_hash, rel_old, new_rel)
        moved += 1
        ep_counter += 1
        if move_logs is not None:
            move_logs.append(f"{target_root / rel_old} → {target_root / new_rel}")

    return show, moved

async def import_movie(torrent_name: str, info_hash: str, content_root: Path, move_logs: List[str] | None = None) -> Tuple[str, Path]:
    r"""
    Déplace le plus gros fichier vidéo en:
    movies\Title (Year)\Title (Year).ext
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
    ensure_dir(movie_dir)
    await qbit.set_location(info_hash, movie_dir)

    new_filename = f"{display}{video.suffix.lower()}"
    rel_old = video.relative_to(content_root) if content_root.is_dir() else Path(video.name)
    new_rel = unique_rel_path(movie_dir, Path(new_filename))
    await qbit.rename_file(info_hash, rel_old, new_rel)
    new_path = movie_dir / new_rel

    if move_logs is not None:
        move_logs.append(f"{movie_dir / rel_old} → {new_path}")

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


class AutoImportView(discord.ui.View):
    def __init__(self, info_hash: str, user_id: int, label: str, *, timeout: float = 90):
        super().__init__(timeout=timeout)
        self.info_hash = info_hash
        self.user_id = user_id
        self.label = label

    def _cleanup(self):
        pending_import_prompts.discard(self.info_hash)

    async def on_timeout(self) -> None:  # type: ignore[override]
        self._cleanup()
        return await super().on_timeout()

    @discord.ui.button(label="Importer maintenant", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Seul l'utilisateur qui a ajouté ce torrent peut l'importer.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await handle_auto_import(interaction, self.info_hash, self.label)
        self._cleanup()
        self.stop()

    @discord.ui.button(label="Plus tard", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if interaction.response.is_done():
            return
        await interaction.response.edit_message(content=f"Import différé pour `{self.label}`.", view=None)
        self._cleanup()
        self.stop()


class RssSelect(discord.ui.Select):
    def __init__(self, items: List[Dict[str, str]], category: str):
        options = []
        for idx, item in enumerate(items):
            label = shorten(item["title"], 90)
            options.append(discord.SelectOption(label=label, value=str(idx)))
        super().__init__(placeholder="Choisis un torrent à ajouter", options=options, min_values=1, max_values=1)
        self.items = items
        self.category = category

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        idx = int(self.values[0])
        item = self.items[idx]
        url = item.get("enclosure") or item.get("link")
        if not url:
            await interaction.response.send_message("❌ Lien torrent introuvable dans le flux.", ephemeral=True)
            return
        try:
            await qbit.add_magnet(url, category=self.category)
            added_via = "URL"
        except Exception as e:
            # Fallback: télécharger le .torrent et l'uploader en multipart
            try:
                logger.info("URL add failed (%s). Waiting %ss before fallback download.", e, JACKETT_COOLDOWN_SECONDS)
                await asyncio.sleep(JACKETT_COOLDOWN_SECONDS)
                timeout = aiohttp.ClientTimeout(total=25)
                headers = {"User-Agent": JACKETT_USER_AGENT} if JACKETT_USER_AGENT else {}
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            raise RuntimeError(f"HTTP {resp.status} lors du téléchargement du .torrent ({text[:120]})")
                        torrent_bytes = await resp.read()
                await qbit.add_torrent_file(torrent_bytes, filename="download.torrent", category=self.category)
                added_via = "upload .torrent"
            except Exception as e2:
                logger.error("qBittorrent add failed via URL (%s) and upload (%s).", e, e2)
                await interaction.response.send_message(
                    f"❌ Ajout qBittorrent échoué via URL ({e}) et via upload ({e2}).",
                    ephemeral=True,
                )
                return

        logger.info("Added torrent '%s' via %s (category=%s).", item.get("title", "???"), added_via, self.category)
        await interaction.response.send_message(
            f"✅ Ajouté: `{item.get('title', '???')}` (catégorie: {self.category}, méthode: {added_via})",
            ephemeral=True,
        )

        # Tentative de retrouver le hash pour le suivi
        try:
            recent = await qbit.list_torrents(limit=30)
            found = find_torrent_by_name_sync(recent, item.get("title", ""))
            if found and found.get("hash"):
                remember_tracked_torrent(found["hash"], interaction.user)
        except Exception:
            pass


class RssView(discord.ui.View):
    def __init__(self, items: List[Dict[str, str]], category: str, *, timeout: float = 120):
        super().__init__(timeout=timeout)
        if items:
            self.add_item(RssSelect(items, category))


async def send_jackett_results(interaction: discord.Interaction, query: str, limit: int, kind: str):
    search_url = build_jackett_search_url(query, limit=max(limit, 1))
    try:
        xml_text = await fetch_rss(search_url)
    except Exception as e:
        await interaction.followup.send(f"❌ Impossible d'interroger Jackett: {e}", ephemeral=True)
        logger.error("Jackett search failed for query=%s: %s", query, e)
        return

    try:
        items = parse_rss_feed(xml_text, limit=limit)
    except ET.ParseError as e:
        await interaction.followup.send(f"❌ Flux RSS invalide: {e}", ephemeral=True)
        return
    if not items:
        await interaction.followup.send("Aucun résultat pour cette recherche.", ephemeral=True)
        return

    lines = []
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item['title']} (`{item.get('pub_date','')}`)")

    embed = discord.Embed(
        title=f"Jackett ({JACKETT_INDEXER}): résultats pour \"{query}\"",
        description="\n".join(lines),
    )
    embed.set_footer(text="Sélectionne dans la liste pour ajouter à qBittorrent.")

    category = "movies" if kind.lower().strip() == "movies" else "series"
    view = RssView(items, category)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


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
            if info_hash in pending_import_prompts:
                return
            pending_import_prompts.add(info_hash)
            view = AutoImportView(info_hash, interaction.user.id, label)
            await interaction.followup.send(
                f"📦 Télécharger terminé pour `{label}`. Importer maintenant ?",
                view=view,
                ephemeral=True,
            )
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


@bot.tree.command(name="rssfeed", description="Consulte un flux RSS Jackett et ajoute un torrent.")
@app_commands.describe(
    url="URL du flux RSS Jackett (par défaut JACKETT_RSS_URL)",
    limit="Nombre d'items à afficher (défaut 5)",
    kind="movies ou series pour choisir la catégorie qBittorrent",
)
async def rssfeed(interaction: discord.Interaction, url: str | None = None, limit: int = 5, kind: str = "movies"):
    await interaction.response.defer(ephemeral=True)
    feed_url = url or JACKETT_RSS_URL
    if not feed_url:
        await interaction.followup.send("❌ Aucun flux RSS Jackett configuré (renseigne JACKETT_RSS_URL).", ephemeral=True)
        return
    logger.info("RSS feed requested: url=%s limit=%s kind=%s", feed_url, limit, kind)
    try:
        xml_text = await fetch_rss(feed_url)
    except Exception as e:
        logger.error("RSS fetch failed for %s: %s", feed_url, e)
        await interaction.followup.send(f"❌ Impossible de lire le flux RSS: {e}", ephemeral=True)
        return

    try:
        items = parse_rss_feed(xml_text, limit=limit)
    except ET.ParseError as e:
        await interaction.followup.send(f"❌ Flux RSS invalide: {e}", ephemeral=True)
        return
    if not items:
        await interaction.followup.send("Flux RSS vide ou non lisible.", ephemeral=True)
        return

    lines = []
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item['title']} (`{item.get('pub_date','')}`)")

    embed = discord.Embed(
        title="Flux RSS (Jackett)",
        description="\n".join(lines),
    )
    embed.set_footer(text="Sélectionne dans la liste pour ajouter à qBittorrent.")

    category = "movies" if kind.lower().strip() == "movies" else "series"
    view = RssView(items, category)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="jackettsearch", description="Recherche via Jackett et ajoute un torrent.")
@app_commands.describe(
    query="Texte à rechercher",
    limit="Nombre d'items à afficher (défaut 5)",
    kind="movies ou series pour choisir la catégorie qBittorrent",
)
async def jackettsearch(
    interaction: discord.Interaction,
    query: str,
    limit: int = 5,
    kind: str = "movies",
):
    await interaction.response.defer(ephemeral=True)
    if not JACKETT_API_KEY:
        await interaction.followup.send("❌ JACKETT_API_KEY manquant dans l'environnement.", ephemeral=True)
        return
    logger.info("Jackett search: indexer=%s query=%s limit=%s kind=%s", JACKETT_INDEXER, query, limit, kind)
    await send_jackett_results(interaction, query, limit, kind)


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
        if info_hash:
            remember_tracked_torrent(info_hash, interaction.user)
            tracked_msg = f" (suivi pour {interaction.user.display_name or interaction.user.name})"
        else:
            tracked_msg = " (hash non détecté : suivi limité)"
        await interaction.followup.send(f"✅ Magnet ajouté (catégorie: {category}){tracked_msg}.", ephemeral=True)
        if track:
            if info_hash:
                label = f"{category} ({info_hash[:8]})"
                asyncio.create_task(track_download_progress(interaction, info_hash, label))
            else:
                await interaction.followup.send("⚠️ Suivi automatique indisponible (hash introuvable dans le magnet).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur qBittorrent : {e}", ephemeral=True)


@bot.tree.command(name="cleartorrents", description="Réinitialise la liste virtuelle des torrents suivis.")
async def cleartorrents(interaction: discord.Interaction):
    tracked_torrents.clear()
    imported_torrents.clear()
    pending_import_prompts.clear()
    await interaction.response.send_message("🧹 Liste des torrents suivis réinitialisée. Les imports futurs concerneront uniquement les nouveaux torrents ajoutés.", ephemeral=True)


async def handle_auto_import(interaction: discord.Interaction, info_hash: str, label: str):
    try:
        torrent = await qbit.get_torrent_by_hash(info_hash)
    except Exception as e:
        await interaction.followup.send(f"❌ Impossible de récupérer le torrent `{label}` : {e}", ephemeral=True)
        return

    if not torrent:
        await interaction.followup.send(f"❌ Torrent `{label}` introuvable dans qBittorrent.", ephemeral=True)
        return

    ok, reason = check_user_ownership(info_hash, interaction.user.id)
    if not ok:
        await interaction.followup.send(f"❌ Import refusé pour `{label}` : {reason}.", ephemeral=True)
        return

    progress = float(torrent.get("progress", 0.0))
    if progress < 1.0:
        await interaction.followup.send(f"⏳ `{label}` n'est pas encore terminé ({int(progress * 100)}%).", ephemeral=True)
        return

    move_logs: List[str] = []
    try:
        msg, did_series, did_movies, moved_files, _ = await import_torrent_entry(torrent, move_logs)
    except Exception as e:
        await interaction.followup.send(f"❌ Import échoué pour `{label}` : {e}", ephemeral=True)
        return

    imported_torrents.add(info_hash)

    refresh_lines = []
    if did_movies and PLEX_MOVIES_SECTION_ID:
        ok_refresh, msg_refresh = await plex_refresh(PLEX_MOVIES_SECTION_ID)
        refresh_lines.append(f"🎬 Movies refresh: {'OK' if ok_refresh else 'KO'} ({msg_refresh})")
    if did_series and PLEX_SERIES_SECTION_ID:
        ok_refresh, msg_refresh = await plex_refresh(PLEX_SERIES_SECTION_ID)
        refresh_lines.append(f"📺 Series refresh: {'OK' if ok_refresh else 'KO'} ({msg_refresh})")
    if not refresh_lines and (PLEX_URL and PLEX_TOKEN):
        refresh_lines.append("ℹ️ Plex configuré mais section id manquant.")
    if not (PLEX_URL and PLEX_TOKEN):
        refresh_lines.append("ℹ️ Plex refresh non configuré (scan auto Plex devrait suffire).")

    embed = discord.Embed(
        title=f"📦 Import terminé pour `{label}`",
        description=msg,
    )
    embed.add_field(name="Fichiers déplacés", value=str(moved_files), inline=True)
    if move_logs:
        embed.add_field(name="📂 Copie des fichiers", value="\n".join(move_logs[:12]), inline=False)
    if refresh_lines:
        embed.add_field(name="🔄 Plex", value="\n".join(refresh_lines), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

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
    skipped_full = []
    for t in completed:
        info_hash = t.get("hash", "")
        ok, reason = check_user_ownership(info_hash, interaction.user.id)
        if not ok:
            skipped_full.append(f"{t.get('name', '???')} ({reason})")
            continue

        if info_hash in imported_torrents:
            skipped_full.append(f"{t.get('name', '???')} (déjà importé)")
            continue

        category = (t.get("category") or "").lower().strip()
        content_root = pick_content_path(t)
        if not content_root:
            skipped_full.append(f"{t.get('name', '???')} (chemin introuvable)")
            continue

        is_series = (category == "series") or looks_like_series_name(t.get("name", "???"))
        is_forced_movie = (category == "movies")
        planned.append({
            "torrent": t,
            "name": t.get("name", "???"),
            "content_root": content_root,
            "is_series": is_series and not is_forced_movie,
        })

        if len(planned) >= max_items:
            break

    skipped = skipped_full

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
        torrent = p["torrent"]
        info_hash = torrent.get("hash", "")
        try:
            msg, ds, dm, moved, _ = await import_torrent_entry(torrent, move_logs)
            did_series = did_series or ds
            did_movies = did_movies or dm
            moved_items += 1
            moved_files += moved
            imported_torrents.add(info_hash)
            results.append(msg)
        except Exception as e:
            errors.append(f"{p['name']} ({e})")

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
    total_candidates = len(planned) + len(skipped)
    embed = discord.Embed(
        title="📦 Smart Import Plex",
        description=f"Torrents considérés: **{total_candidates}** | Importés: **{moved_items}** | Fichiers déplacés: **{moved_files}**",
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
