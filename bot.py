import os
import re
import json
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
    f"{JACKETT_URL}/api/v2.0/indexers/c411/results/torznab/api?apikey={JACKETT_API_KEY}&limit=20",
)
JACKETT_USER_AGENT = os.getenv(
    "JACKETT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
)
JACKETT_USER = os.getenv("JACKETT_USER", "")
JACKETT_PASSWORD = os.getenv("JACKETT_PASSWORD", "")
JACKETT_COOKIE_NAME = os.getenv("JACKETT_COOKIE_NAME", "")
JACKETT_COOKIE_VALUE = os.getenv("JACKETT_COOKIE_VALUE", "")
JACKETT_INDEXER = os.getenv("JACKETT_INDEXER", "c411")
JACKETT_COOLDOWN_SECONDS = 30
JACKETT_FORCE_UPLOAD = os.getenv("JACKETT_FORCE_UPLOAD", "false").lower() in {"1", "true", "yes", "on"}

LOG_FILE = os.getenv("BOT_LOG_FILE", "bot.log")
LOG_LEVEL = os.getenv("BOT_LOG_LEVEL", "INFO").upper()
resolved_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=resolved_log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,
)
logger = logging.getLogger("chamoxisbot")
logger.info("Logger initialized (level=%s, file=%s)", LOG_LEVEL, LOG_FILE)

# Ton serveur Discord (sync instant)
GUILD_ID = 369545955252502528

# Extensions vidéo
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts"}

# Patterns séries
SERIES_PATTERNS = [
    re.compile(r"\bS(?P<s>\d{1,2})E(?P<e>\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(?P<s>\d{1,2})x(?P<e>\d{1,3})\b", re.IGNORECASE),
]
SEASON_ONLY_PATTERNS = [
    re.compile(r"\bS(?:aison)?\s*(?P<s>\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bSeason\s*(?P<s>\d{1,2})\b", re.IGNORECASE),
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


class ImportPrefs(TypedDict, total=False):
    kind: str
    target_name: str
    series_mode: str
    season: int
    episode: int


class TrackedTorrent(TypedDict):
    user_id: int
    user_label: str
    prefs: ImportPrefs


tracked_torrents: Dict[str, TrackedTorrent] = {}
imported_torrents: set[str] = set()

MAX_SEARCH_RESULTS = 100
SEASON_SELECT_MAX = 20
EPISODE_SELECT_MAX = 30
KNOWN_USERS_DB = Path("known_users.json")
known_users: set[int] = set()


def load_known_users() -> None:
    global known_users
    if not KNOWN_USERS_DB.exists():
        known_users = set()
        return
    try:
        payload = json.loads(KNOWN_USERS_DB.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Impossible de lire %s", KNOWN_USERS_DB)
        known_users = set()
        return
    if isinstance(payload, list):
        known_users = {int(v) for v in payload if str(v).isdigit()}
    else:
        known_users = set()


def save_known_users() -> None:
    KNOWN_USERS_DB.write_text(
        json.dumps(sorted(known_users), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def register_known_user(user_id: int) -> bool:
    if user_id in known_users:
        return False
    known_users.add(user_id)
    save_known_users()
    return True

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
        logger.info("QbitClient session started for %s", self.base_url)
        self.logged_in = False

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("QbitClient session closed")

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
        logger.info("qBittorrent login successful")

    async def ensure_login(self):
        if not self.logged_in:
            await self.login()

    async def _get_json(self, path: str, params: dict | None = None):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        await self.ensure_login()

        url = f"{self.base_url}{path}"
        logger.debug("qBittorrent GET %s params=%s", path, params)
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
        logger.debug("qBittorrent POST %s data_keys=%s", path, list(data.keys()))
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
        logger.info("qBittorrent add URL (category=%s)", category or "-")
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
            logger.info("qBittorrent upload torrent file (%s bytes, category=%s)", len(torrent_bytes), category or "-")
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


def extract_season_only(text: str) -> Optional[int]:
    for pat in SEASON_ONLY_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group("s"))
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


def _normalize_for_match(text: str) -> str:
    t = normalize_spaces(text).lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def find_recent_torrent_candidate(items: List[dict], *, title: str, category: str) -> Optional[dict]:
    """
    Trouve le torrent le plus probable juste après un ajout Jackett.
    Stratégies:
    1) nom exact
    2) nom normalisé (équivalent / inclusion)
    3) fallback: dernier torrent de même catégorie
    """
    if not items:
        return None

    exact = find_torrent_by_name_sync(items, title)
    if exact:
        return exact

    wanted = _normalize_for_match(title)
    if wanted:
        for t in items:
            n = _normalize_for_match(t.get("name", ""))
            if not n:
                continue
            if n == wanted or wanted in n or n in wanted:
                return t

    same_cat = [t for t in items if (t.get("category") or "").strip().lower() == category.strip().lower()]
    if same_cat:
        same_cat.sort(key=lambda x: int(x.get("added_on", 0)), reverse=True)
        return same_cat[0]

    return None


def remember_tracked_torrent(info_hash: str, user: discord.abc.User, prefs: ImportPrefs | None = None):
    """
    Enregistre un torrent comme suivi pour un utilisateur précis.
    """
    tracked_torrents[info_hash] = {
        "user_id": user.id,
        "user_label": user.display_name or user.name,
        "prefs": prefs or {},
    }


def get_tracked_prefs(info_hash: str) -> ImportPrefs:
    tracked = tracked_torrents.get(info_hash)
    if not tracked:
        return {}
    return tracked.get("prefs", {})


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
        enclosure_length = enclosure.attrib.get("length") if enclosure is not None else ""
        pub_date = (item.findtext("pubDate") or "").strip()
        items.append(
            {
                "title": title,
                "link": link,
                "enclosure": enclosure_url,
                "size": enclosure_length or "",
                "pub_date": pub_date,
            }
        )
        if len(items) >= limit:
            break
    return items


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


def parse_size_bytes(size_str: str) -> int:
    try:
        return int(size_str)
    except Exception:
        return 0


def quality_matches(title: str, quality: str | None) -> bool:
    if not quality:
        return True
    q = quality.lower().strip().replace(" ", "")
    t = title.lower().replace(" ", "")
    return q in t


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


async def download_torrent_with_retry(url: str, *, max_attempts: int = 2) -> bytes:
    """
    Télécharge un .torrent depuis Jackett en gérant auth/cookie + cooldown/retry.
    """
    timeout = aiohttp.ClientTimeout(total=75)
    base_headers = {"User-Agent": JACKETT_USER_AGENT} if JACKETT_USER_AGENT else {}

    async def _attempt(use_auth: bool) -> tuple[int, bytes | None, str]:
        headers = dict(base_headers)
        jar = aiohttp.CookieJar(unsafe=True)
        if use_auth:
            headers.update(_jackett_auth_headers())
            if JACKETT_COOKIE_NAME and JACKETT_COOKIE_VALUE:
                jar.update_cookies({JACKETT_COOKIE_NAME: JACKETT_COOKIE_VALUE})
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookie_jar=jar) as session:
            async with session.get(url, allow_redirects=False) as resp:
                status = resp.status
                if status == 200:
                    return status, await resp.read(), ""
                text = await resp.text()
                loc = resp.headers.get("Location", "")
                extra = f" (redir {loc[:80]})" if loc else ""
                return status, None, f"{text[:120]}{extra}"

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        logger.info("Jackett torrent download attempt %s/%s (auth=none): %s", attempt, max_attempts, url)
        status, payload, detail = await _attempt(False)
        if payload is not None:
            return payload
        logger.warning("Jackett torrent download attempt failed (status=%s, detail=%s)", status, detail)
        if status in (301, 302, 303, 307, 308, 401, 403, 429, 503):
            if JACKETT_USER or (JACKETT_COOKIE_NAME and JACKETT_COOKIE_VALUE):
                logger.info("Jackett torrent download retry (auth=basic/cookie): %s", url)
                status, payload, detail = await _attempt(True)
                if payload is not None:
                    return payload
        if attempt < max_attempts:
            logger.warning("Jackett torrent download failed (HTTP %s). Waiting %ss before retry.", status, JACKETT_COOLDOWN_SECONDS)
            await asyncio.sleep(JACKETT_COOLDOWN_SECONDS)
        last_error = f"HTTP {status} ({detail})"
    raise RuntimeError(f"Téléchargement .torrent échoué: {last_error}")


async def import_torrent_entry(torrent: dict, move_logs: List[str], prefs: ImportPrefs | None = None) -> Tuple[str, bool, bool, int, str]:
    """
    Retourne (message, did_series, did_movies, moved_files, info_hash)
    """
    torrent_name = torrent.get("name", "???")
    category = (torrent.get("category") or "").lower().strip()
    content_root = pick_content_path(torrent)
    if not content_root:
        raise RuntimeError("chemin introuvable")

    info_hash = torrent.get("hash", "")
    prefs = prefs or get_tracked_prefs(info_hash)
    is_series = (category == "series") or looks_like_series_name(torrent_name)
    is_forced_movie = (category == "movies")
    if prefs.get("kind") == "movies":
        is_series = False
        is_forced_movie = True
    elif prefs.get("kind") == "series":
        is_series = True
        is_forced_movie = False
    logger.info(
        "Import entry: name=%s hash=%s category=%s is_series=%s forced_movie=%s source=%s",
        torrent_name,
        info_hash,
        category or "-",
        is_series,
        is_forced_movie,
        content_root,
    )

    if is_series and not is_forced_movie:
        show, n = await import_series(torrent_name, info_hash, content_root, move_logs, prefs)
        target_path = PLEX_SERIES / show
        msg = f"📺 Série: `{torrent_name}` → {n} fichier(s) dans `{target_path}`"
        return msg, True, False, n, info_hash

    display, new_path = await import_movie(torrent_name, info_hash, content_root, move_logs, prefs)
    msg = f"🎬 Film: `{torrent_name}` → `{new_path}`"
    return msg, False, True, 1, info_hash

# ----------------- IMPORT LOGIC -----------------
async def import_series(torrent_name: str, info_hash: str, content_root: Path, move_logs: List[str] | None = None, prefs: ImportPrefs | None = None) -> Tuple[str, int]:
    r"""
    Déplace TOUS les fichiers vidéo de la série:
    series\Show\Season XX\Show - SXXEYY.ext
    Retourne (show_title, nb_fichiers_deplaces)
    """
    prefs = prefs or {}
    show = prefs.get("target_name") or guess_show_title_from_torrent(torrent_name)
    files = all_video_files(content_root)
    if not files:
        raise RuntimeError("aucune vidéo trouvée")
    logger.info("Import series started: torrent=%s hash=%s files=%s source=%s", torrent_name, info_hash, len(files), content_root)

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
    logger.info("Series target location set via qBittorrent: %s", target_root)

    moved = 0
    season_counts: Dict[int, int] = {}

    # 1) fichiers avec S/E
    series_mode = prefs.get("series_mode", "complete")
    forced_season = int(prefs.get("season", 0) or 0)
    forced_episode = int(prefs.get("episode", 0) or 0)

    for season, ep, f in known:
        season_counts[season] = season_counts.get(season, 0) + 1
        use_season = forced_season if forced_season > 0 else season
        use_episode = forced_episode if (series_mode == "single" and forced_episode > 0) else ep
        season_dir = Path(f"S{use_season:02d}")
        new_filename = f"{show} - S{use_season:02d}E{use_episode:02d}{f.suffix.lower()}"
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
        use_season = forced_season if forced_season > 0 else guessed_season
        use_episode = forced_episode if (series_mode == "single" and forced_episode > 0) else ep_counter
        season_dir = Path(f"S{use_season:02d}")
        new_filename = f"{show} - S{use_season:02d}E{use_episode:02d}{f.suffix.lower()}"
        new_rel = unique_rel_path(target_root, season_dir / new_filename)
        ensure_dir(target_root / new_rel.parent)
        rel_old = f.relative_to(content_root) if content_root.is_dir() else Path(f.name)
        await qbit.rename_file(info_hash, rel_old, new_rel)
        moved += 1
        ep_counter += 1
        if move_logs is not None:
            move_logs.append(f"{target_root / rel_old} → {target_root / new_rel}")

    logger.info("Import series completed: show=%s moved=%s", show, moved)
    return show, moved

async def import_movie(torrent_name: str, info_hash: str, content_root: Path, move_logs: List[str] | None = None, prefs: ImportPrefs | None = None) -> Tuple[str, Path]:
    r"""
    Déplace le plus gros fichier vidéo en:
    movies\Title (Year)\Title (Year).ext
    Retourne (display_name, new_path)
    """
    files = all_video_files(content_root)
    if not files:
        raise RuntimeError("aucune vidéo trouvée")
    logger.info("Import movie started: torrent=%s hash=%s files=%s source=%s", torrent_name, info_hash, len(files), content_root)

    video = pick_biggest_video(files)
    if not video:
        raise RuntimeError("aucune vidéo trouvée")

    prefs = prefs or {}
    if prefs.get("target_name"):
        display = prefs["target_name"]
    else:
        title, year = build_movie_title_and_year(video.stem)
        display = f"{title} ({year})" if year else title

    movie_dir = PLEX_MOVIES / display
    ensure_dir(movie_dir)
    await qbit.set_location(info_hash, movie_dir)
    logger.info("Movie target location set via qBittorrent: %s", movie_dir)

    new_filename = f"{display}{video.suffix.lower()}"
    rel_old = video.relative_to(content_root) if content_root.is_dir() else Path(video.name)
    new_rel = unique_rel_path(movie_dir, Path(new_filename))
    await qbit.rename_file(info_hash, rel_old, new_rel)
    new_path = movie_dir / new_rel

    if move_logs is not None:
        move_logs.append(f"{movie_dir / rel_old} → {new_path}")

    logger.info("Import movie completed: display=%s path=%s", display, new_path)
    return display, new_path


def is_av1_title(title: str) -> bool:
    return "av1" in title.lower()


def sanitize_series_title(name: str) -> str:
    base = strip_brackets(name)
    for pat in SERIES_PATTERNS:
        m = pat.search(base)
        if m:
            base = base[:m.start()]
            break
    base = remove_junk_tokens(base)
    return normalize_spaces(base).strip(" -._")


def suggest_target_directories(query: str, kind: str, *, max_items: int = 10) -> List[str]:
    needle = sanitize_series_title(query) if kind == "series" else remove_junk_tokens(strip_brackets(query))
    norm_needle = _normalize_for_match(needle)
    root = PLEX_SERIES if kind == "series" else PLEX_MOVIES
    if not root.exists() or not root.is_dir():
        return []

    scored: List[tuple[int, str]] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        n = _normalize_for_match(d.name)
        if not n:
            continue
        score = 0
        if norm_needle and (norm_needle in n or n in norm_needle):
            score = 3
        elif norm_needle:
            tokens = [t for t in re.split(r"\W+", needle.lower()) if t]
            overlap = sum(1 for t in tokens if t and t in d.name.lower())
            if overlap:
                score = 1 + min(overlap, 2)
        if score > 0:
            scored.append((score, d.name))

    scored.sort(key=lambda x: (-x[0], x[1].lower()))
    names = [name for _, name in scored[:max_items]]
    if not names:
        return []
    dedup: List[str] = []
    for n in names:
        if n not in dedup:
            dedup.append(n)
    return dedup


def extract_quality_options(items: List[Dict[str, str]]) -> List[str]:
    qualities: List[str] = []
    for it in items:
        title = it.get("title", "")
        for q in ("2160p", "1080p", "720p", "480p"):
            if q.lower() in title.lower() and q not in qualities:
                qualities.append(q)
    return qualities


def default_mode_from_query(query: str) -> tuple[str, int, int]:
    se = extract_season_episode(query)
    if se:
        return "series", se[0], se[1]
    season_only = extract_season_only(query)
    if season_only:
        return "series", season_only, 0
    return "movies", 0, 0



class RssSelect(discord.ui.Select):
    def __init__(self, items: List[Dict[str, str]], category: str, prefs: ImportPrefs | None = None, track: bool = True):
        options = []
        for idx, item in enumerate(items):
            label = shorten(item["title"], 90)
            options.append(discord.SelectOption(label=label, value=str(idx)))
        super().__init__(placeholder="Choisis un torrent à ajouter", options=options, min_values=1, max_values=1)
        self.items = items
        self.category = category
        self.prefs = prefs or {}
        self.track = track

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        logger.info("RSS selection callback by %s (%s)", interaction.user, interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        idx = int(self.values[0])
        item = self.items[idx]
        url = item.get("enclosure") or item.get("link")
        if not url:
            await interaction.followup.send("❌ Lien torrent introuvable dans le flux.", ephemeral=True)
            return
        added_via = "URL"
        if not JACKETT_FORCE_UPLOAD:
            try:
                await qbit.add_magnet(url, category=self.category)
                added_via = "URL"
            except Exception as e:
                logger.warning("URL add failed (%r). Falling back to .torrent upload.", e)
                added_via = "upload .torrent"
        else:
            added_via = "upload .torrent"

        if added_via == "upload .torrent":
            try:
                torrent_bytes = await download_torrent_with_retry(url)
                await qbit.add_torrent_file(torrent_bytes, filename="download.torrent", category=self.category)
            except Exception as e2:
                logger.exception("qBittorrent add failed via upload: %r", e2)
                await interaction.followup.send(
                    f"❌ Ajout qBittorrent échoué via upload ({e2!r}).",
                    ephemeral=True,
                )
                return

        logger.info("Added torrent '%s' via %s (category=%s).", item.get("title", "???"), added_via, self.category)
        await interaction.followup.send(
            f"✅ Ajouté: `{item.get('title', '???')}` (catégorie: {self.category}, méthode: {added_via})",
            ephemeral=True,
        )

        # Tentative de retrouver le hash pour le suivi
        try:
            recent = await qbit.list_torrents(limit=30)
            found = find_recent_torrent_candidate(
                recent,
                title=item.get("title", ""),
                category=self.category,
            )
            if found and found.get("hash"):
                h = found["hash"]
                remember_tracked_torrent(h, interaction.user, self.prefs)
                label = found.get("name") or item.get("title", "torrent")
                if self.track:
                    asyncio.create_task(track_download_progress(interaction, h, label))
                else:
                    asyncio.create_task(auto_import_when_complete(interaction, h, label))
            else:
                logger.warning(
                    "No matching torrent hash found right after add, scheduling broad fallback auto-import. title=%s category=%s",
                    item.get("title", ""),
                    self.category,
                )
                asyncio.create_task(auto_import_latest_for_user(interaction, interaction.user.id, self.category, self.prefs, item.get("title", "torrent")))
        except Exception:
            logger.exception("Failed to track torrent after RSS add")


class RssView(discord.ui.View):
    def __init__(self, items: List[Dict[str, str]], category: str, prefs: ImportPrefs | None = None, track: bool = True, *, timeout: float = 120):
        super().__init__(timeout=timeout)
        if items:
            self.add_item(RssSelect(items, category, prefs=prefs, track=track))


async def send_jackett_results(
    interaction: discord.Interaction,
    query: str,
    kind: str,
    prefs: ImportPrefs | None = None,
    track: bool = True,
    quality: str | None = None,
):
    search_url = build_jackett_search_url(query, limit=MAX_SEARCH_RESULTS)
    try:
        xml_text = await fetch_rss(search_url)
    except Exception as e:
        await interaction.followup.send(f"❌ Impossible d'interroger Jackett: {e}", ephemeral=True)
        logger.error("Jackett search failed for query=%s: %s", query, e)
        return

    try:
        items = parse_rss_feed(xml_text, limit=MAX_SEARCH_RESULTS)
    except ET.ParseError as e:
        await interaction.followup.send(f"❌ Flux RSS invalide: {e}", ephemeral=True)
        return

    items = [it for it in items if not is_av1_title(it.get("title", ""))]
    if quality:
        items = [it for it in items if quality_matches(it.get("title", ""), quality)]

    items.sort(key=lambda it: parse_size_bytes(it.get("size", "")), reverse=True)

    if not items:
        await interaction.followup.send("Aucun résultat pour cette recherche.", ephemeral=True)
        return

    lines = []
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item['title']} — {human_size(item.get('size', ''))} (`{item.get('pub_date','')}`)")

    embed = discord.Embed(
        title=f"Jackett ({JACKETT_INDEXER}): résultats pour \"{query}\"",
        description="\n".join(lines[:25]),
    )
    footer = f"Tri: poids décroissant | AV1 exclu | Filtre qualité: {quality or 'aucun'}"
    if len(lines) > 25:
        footer += f" | {len(lines) - 25} résultat(s) supplémentaire(s) non affiché(s)"
    embed.set_footer(text=footer)

    category = "movies" if kind.lower().strip() == "movies" else "series"
    view = RssView(items, category, prefs=prefs, track=track)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def track_download_progress(interaction: discord.Interaction, info_hash: str, label: str):
    """
    Suit le téléchargement et met à jour un message ephemeral.
    """
    message = await interaction.followup.send(f"📥 Suivi de `{label}`…", ephemeral=True)
    logger.info("Tracking download started: hash=%s label=%s user=%s", info_hash, label, interaction.user.id)
    max_iterations = 120  # ~10 minutes avec sleep(5)
    for _ in range(max_iterations):
        try:
            info = await qbit.get_torrent_by_hash(info_hash)
        except Exception as e:
            logger.exception("Tracking download failed for hash=%s", info_hash)
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
            await handle_auto_import(interaction, info_hash, label)
            return
        await asyncio.sleep(5)

    await message.edit(content="⚠️ Suivi arrêté après délai — dernier état affiché.")


async def auto_import_when_complete(interaction: discord.Interaction, info_hash: str, label: str):
    """
    Version silencieuse: attend la fin du torrent puis lance l'import auto.
    """
    for _ in range(240):  # ~20 min
        try:
            info = await qbit.get_torrent_by_hash(info_hash)
        except Exception:
            logger.exception("Silent auto-import polling failed for hash=%s", info_hash)
            return
        if not info:
            return
        progress = float(info.get("progress", 0.0))
        state = str(info.get("state", "")).lower()
        if progress >= 1.0 or state.startswith("upload") or state.startswith("stalledup"):
            await handle_auto_import(interaction, info_hash, label)
            return
        await asyncio.sleep(5)


async def auto_import_latest_for_user(
    interaction: discord.Interaction,
    user_id: int,
    category: str,
    prefs: ImportPrefs,
    label_hint: str,
):
    """
    Fallback ultime si on n'arrive pas à mapper immédiatement le hash ajouté.
    Cherche le dernier torrent de la catégorie, l'associe à l'utilisateur, puis attend la fin pour importer.
    """
    try:
        await asyncio.sleep(3)
        recent = await qbit.list_torrents(limit=30)
    except Exception:
        logger.exception("Fallback lookup failed (unable to list torrents)")
        return

    candidate = find_recent_torrent_candidate(recent, title=label_hint, category=category)
    if not candidate or not candidate.get("hash"):
        logger.warning("Fallback lookup found no candidate for label=%s category=%s", label_hint, category)
        return

    h = candidate["hash"]
    fake_user = interaction.user
    if fake_user.id != user_id:
        return
    remember_tracked_torrent(h, fake_user, prefs)
    logger.info("Fallback mapped torrent hash=%s name=%s for user=%s", h, candidate.get("name", "?"), user_id)
    await auto_import_when_complete(interaction, h, candidate.get("name") or label_hint)


class TorrentKindView(discord.ui.View):
    def __init__(self, query: str, season: int, episode: int, quality: str | None = None):
        super().__init__(timeout=180)
        self.query = query
        self.detected_season = season
        self.detected_episode = episode
        self.quality = quality

    async def _start(self, interaction: discord.Interaction, kind: str):
        sugg = suggest_target_directories(self.query, kind)
        await interaction.response.send_message(
            "Choisis les options de recherche:",
            ephemeral=True,
            view=TorrentOptionView(
                query=self.query,
                kind=kind,
                suggested_dirs=sugg,
                season=self.detected_season,
                episode=self.detected_episode,
                quality=self.quality,
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
    ):
        super().__init__(timeout=300)
        self.query = query
        self.kind = kind
        self.series_mode = "complete"
        self.season = season
        self.episode = episode
        self.target_name: str | None = None
        self.quality = quality

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


    @discord.ui.button(label="✅ Confirmer et chercher", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.target_name:
            await interaction.response.send_message(
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

        await interaction.response.send_message(
            "Configuration validée. Lancement de la recherche...\n" + "\n".join(summary),
            ephemeral=True,
        )
        await send_jackett_results(
            interaction,
            self.query,
            self.kind,
            prefs=prefs,
            track=True,
            quality=self.quality,
        )


class TorrentDirSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Nom du répertoire cible", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            selected = self.values[0]
            if selected == "__manual__":
                await interaction.response.send_modal(TorrentManualDirModal(parent))
                return
            parent.target_name = selected
        await interaction.response.defer()


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
            await interaction.response.defer()
            return
        await interaction.response.send_modal(TorrentManualDirModal(parent))


class TorrentSeriesModeSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Mode série", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            parent.series_mode = self.values[0]
        await interaction.response.defer()


class TorrentSeasonSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Saison", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            parent.season = int(self.values[0])
        await interaction.response.defer()



class TorrentQualitySelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], selected: str):
        for o in options:
            o.default = (o.value == selected)
        super().__init__(placeholder="Qualité", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):  # type: ignore[override]
        parent = self.view
        if isinstance(parent, TorrentOptionView):
            parent.quality = None if self.values[0] == "all" else self.values[0]
        await interaction.response.defer()



# ----------------- DISCORD EVENTS -----------------
@bot.event
async def setup_hook():
    load_known_users()
    await qbit.start()
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    logger.info("Synced %s command(s) to guild %s: %s", len(synced), GUILD_ID, [c.name for c in synced])

@bot.event
async def on_ready():
    logger.info("Connecté en tant que %s", bot.user)


@bot.event
async def on_error(event: str, *args, **kwargs):
    logger.exception("Unhandled Discord event error on %s", event)




@bot.tree.interaction_check
async def ensure_user_has_read_info(interaction: discord.Interaction) -> bool:
    command_name = getattr(getattr(interaction, "command", None), "qualified_name", "")
    if command_name == "info":
        return True
    if interaction.user.id in known_users:
        return True

    msg = (
        "👋 Première utilisation détectée. Pour continuer, lance d'abord `/info`.\n"
        "Cette étape t'explique le fonctionnement du bot et t'enregistre automatiquement."
    )
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.exception(
        "App command error: command=%s user=%s(%s) guild=%s",
        getattr(getattr(interaction, "command", None), "qualified_name", "unknown"),
        getattr(interaction.user, "name", "unknown"),
        getattr(interaction.user, "id", "unknown"),
        getattr(getattr(interaction, "guild", None), "id", "DM"),
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Erreur de commande: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Erreur de commande: {error}", ephemeral=True)
    except Exception:
        logger.exception("Failed to send app command error message to Discord")

# ----------------- COMMANDS -----------------
@bot.tree.command(name="status", description="Liste les derniers torrents qBittorrent.")
async def status(interaction: discord.Interaction):
    logger.info("/status called by %s (%s)", interaction.user, interaction.user.id)
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
        logger.exception("/status failed")
        await interaction.followup.send(f"❌ Erreur qBittorrent : {e}", ephemeral=True)


@bot.tree.command(name="rssfeed", description="Consulte un flux RSS Jackett et ajoute un torrent.")
@app_commands.describe(
    url="URL du flux RSS Jackett (par défaut JACKETT_RSS_URL)",
    limit="Nombre d'items à afficher (défaut 5)",
    kind="movies ou series pour choisir la catégorie qBittorrent",
)
async def rssfeed(interaction: discord.Interaction, url: str | None = None, limit: int = 5, kind: str = "movies"):
    logger.info("/rssfeed called by %s (%s)", interaction.user, interaction.user.id)
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

    items.sort(key=lambda it: parse_size_bytes(it.get("size", "")), reverse=True)
    if limit > 0:
        items = items[:limit]

    lines = []
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item['title']} — {human_size(item.get('size', ''))} (`{item.get('pub_date','')}`)")

    embed = discord.Embed(
        title="Flux RSS (Jackett)",
        description="\n".join(lines),
    )
    embed.set_footer(text="Sélectionne dans la liste pour ajouter à qBittorrent.")

    category = "movies" if kind.lower().strip() == "movies" else "series"
    view = RssView(items, category, track=True)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="recherchetorrent", description="Assistant interactif de recherche torrent.")
@app_commands.describe(
    query="Titre à chercher (ex: Friends ou Friends.S01E01)",
)
async def recherchetorrent(
    interaction: discord.Interaction,
    query: str,
):
    logger.info("/recherchetorrent called by %s (%s)", interaction.user, interaction.user.id)
    if not JACKETT_API_KEY:
        await interaction.response.send_message("❌ JACKETT_API_KEY manquant dans l'environnement.", ephemeral=True)
        return

    default_kind, season, episode = default_mode_from_query(query)

    content = (
        "Choisis Film / Série puis renseigne les options (dossier cible, saison/épisode), "
        "et confirme pour lancer la recherche.\n"
        f"Détection automatique: {'Série' if default_kind == 'series' else 'Film'}"
    )

    await interaction.response.send_message(
        content,
        view=TorrentKindView(query, season, episode, quality=None),
        ephemeral=True,
    )



@bot.tree.command(name="addmagnet", description="Ajoute un magnet à qBittorrent (usage légal).")
@app_commands.describe(
    magnet="Lien magnet",
    kind="movies ou series",
    track="Suivre automatiquement la progression du téléchargement",
)
async def addmagnet(interaction: discord.Interaction, magnet: str, kind: str = "movies", track: bool = True):
    logger.info("/addmagnet called by %s (%s), kind=%s, track=%s", interaction.user, interaction.user.id, kind, track)
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
        logger.exception("/addmagnet failed")
        await interaction.followup.send(f"❌ Erreur qBittorrent : {e}", ephemeral=True)


@bot.tree.command(name="cleartorrents", description="Réinitialise la liste virtuelle des torrents suivis.")
async def cleartorrents(interaction: discord.Interaction):
    logger.info("/cleartorrents called by %s (%s)", interaction.user, interaction.user.id)
    tracked_torrents.clear()
    imported_torrents.clear()
    await interaction.response.send_message("🧹 Liste des torrents suivis réinitialisée. Les imports futurs concerneront uniquement les nouveaux torrents ajoutés.", ephemeral=True)


@bot.tree.command(name="info", description="Guide complet d'utilisation du bot (obligatoire à la 1re utilisation).")
async def info_cmd(interaction: discord.Interaction):
    newly_registered = register_known_user(interaction.user.id)
    logger.info("/info called by %s (%s), newly_registered=%s", interaction.user, interaction.user.id, newly_registered)

    text = (
        "**Bienvenue sur ChamoxisBOT 👋**\n\n"
        "Ce bot sert à chercher des torrents via Jackett, les ajouter dans qBittorrent, "
        "suivre le téléchargement puis ranger automatiquement les fichiers pour Plex.\n\n"
        "**Étape 1 — Commande principale**\n"
        "- Lance `/recherchetorrent query:<ton titre>` (ex: `andor s02`, `dune part two`).\n"
        "- Le bot te demande ensuite Film ou Série.\n"
        "- Tu choisis (ou saisis) le **répertoire cible exact** (ex: `Andor (2022)`).\n"
        "- Pour les séries, tu peux préciser le mode (complet/épisode) et la saison.\n"
        "- Tu confirmes puis tu sélectionnes le résultat qui t'intéresse.\n\n"
        "**Ce que fait le bot ensuite**\n"
        "1) Ajoute le torrent dans qBittorrent.\n"
        "2) Suit la progression automatiquement.\n"
        "3) À 100%, range les fichiers dans les bons dossiers Plex (films/séries).\n\n"
        "**Autres commandes utiles**\n"
        "- `/status` : voir les derniers torrents et leur état.\n"
        "- `/rssfeed` : lire un flux RSS Jackett et ajouter un item rapidement.\n"
        "- `/addmagnet` : ajouter un lien magnet manuellement.\n"
        "- `/cleartorrents` : réinitialiser la mémoire des torrents suivis.\n\n"
        "**Exemples simples**\n"
        "- Film: `/recherchetorrent query:gremlins 2` puis dossier `Gremlins 2 (1990)`.\n"
        "- Série: `/recherchetorrent query:andor s02` puis dossier `Andor (2022)`.\n\n"
        "**Important**\n"
        "- Le bot est réservé à un usage légal.\n"
        "- Si un import est refusé, vérifie que c'est bien ton torrent (protection par utilisateur)."
    )

    prefix = "✅ Ton compte est maintenant enregistré, tu peux utiliser toutes les commandes.\n\n" if newly_registered else "ℹ️ Ton compte est déjà enregistré.\n\n"
    await interaction.response.send_message(prefix + text, ephemeral=True)


async def handle_auto_import(interaction: discord.Interaction, info_hash: str, label: str):
    logger.info("Auto-import requested for hash=%s label=%s by %s", info_hash, label, interaction.user.id)
    try:
        torrent = await qbit.get_torrent_by_hash(info_hash)
    except Exception as e:
        logger.exception("Auto-import failed while fetching torrent")
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
        msg, did_series, did_movies, moved_files, _ = await import_torrent_entry(torrent, move_logs, get_tracked_prefs(info_hash))
    except Exception as e:
        logger.exception("Auto-import failed while importing torrent")
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

# ----------------- MAIN -----------------
async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN manquant dans .env")
    logger.info("Starting bot...")
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        logger.info("Stopping bot...")
        await qbit.close()

if __name__ == "__main__":
    asyncio.run(main())
