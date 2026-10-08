"""Historical search operations extracted without changing their behavior."""
from modules.plex.config import *

def parse_rss_feed(xml_text: str, limit: int = 10, source: str | None = None) -> List[Dict[str, str]]:
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
        seeders = ""
        peers = ""
        grabs = ""
        for child in item:
            tag = child.tag.split("}")[-1].lower()
            if tag != "attr":
                continue
            name = str(child.attrib.get("name", "")).lower()
            value = str(child.attrib.get("value", "")).strip()
            if name == "seeders":
                seeders = value
            elif name in {"peers", "leechers"}:
                peers = value
            elif name in {"grabs", "downloads"}:
                grabs = value
        items.append(
            {
                "title": title,
                "link": link,
                "enclosure": enclosure_url,
                "size": enclosure_length or "",
                "pub_date": pub_date,
                "source": source or "",
                "seeders": seeders,
                "peers": peers,
                "grabs": grabs,
            }
        )
        if len(items) >= limit:
            break
    return items


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


def build_torznab_search_url(query: str, limit: int = 20, indexer_id: str | None = None) -> str:
    q = urllib.parse.quote_plus(query)
    idx = urllib.parse.quote(str(indexer_id or PROWLARR_INDEXER_ID), safe="")
    base = f"{PROWLARR_URL}/api/v1/indexer/{idx}/newznab/"
    return f"{base}?apikey={PROWLARR_API_KEY}&t=search&q={q}&limit={limit}"


def _torznab_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if TORZNAB_USER_AGENT:
        headers["User-Agent"] = TORZNAB_USER_AGENT
    return headers


async def torznab_request(url: str, *, expect_json: bool = False) -> str | list | dict:
    """
    Fait une requête GET Torznab, tente d'abord sans auth, puis avec Basic Auth / cookie si configuré,
    et gère explicitement les 302/401/403.
    """
    timeout = aiohttp.ClientTimeout(total=25)
    base_headers = {"User-Agent": TORZNAB_USER_AGENT} if TORZNAB_USER_AGENT else {}

    async def _attempt(use_auth: bool):
        headers = dict(base_headers)
        jar = aiohttp.CookieJar(unsafe=True)
        if use_auth:
            headers.update(_torznab_headers())
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
    logger.info("Torznab GET %s (auth=none)", url)
    result = await _attempt(False)
    if result is not None:
        return result

    # Second essai avec Basic Auth / cookie si dispo
    logger.error("Torznab access refused for %s", url)
    raise RuntimeError("Accès Torznab refusé.")


async def fetch_rss(url: str, *, user_agent: str = TORZNAB_USER_AGENT) -> str:
    """
    Récupère un flux RSS générique (utilisé pour Torznab).
    """
    # user_agent param conservé pour compat mais on passe par torznab_request pour gérer l'auth/cookies.
    return str(await torznab_request(url, expect_json=False))


async def download_torrent_with_retry(url: str, *, max_attempts: int = 2) -> bytes:
    """
    Télécharge un .torrent depuis Torznab en gérant auth/cookie + cooldown/retry.
    """
    timeout = aiohttp.ClientTimeout(total=75)
    base_headers = {"User-Agent": TORZNAB_USER_AGENT} if TORZNAB_USER_AGENT else {}

    async def _attempt(use_auth: bool) -> tuple[int, bytes | None, str]:
        headers = dict(base_headers)
        jar = aiohttp.CookieJar(unsafe=True)
        if use_auth:
            headers.update(_torznab_headers())
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
        logger.info("Torznab torrent download attempt %s/%s (auth=none): %s", attempt, max_attempts, url)
        status, payload, detail = await _attempt(False)
        if payload is not None:
            return payload
        logger.warning("Torznab torrent download attempt failed (status=%s, detail=%s)", status, detail)
        if status in (301, 302, 303, 307, 308, 401, 403, 429, 503):
            logger.info("Torznab torrent download retry: %s", url)
            status, payload, detail = await _attempt(True)
            if payload is not None:
                return payload
        if attempt < max_attempts:
            logger.warning("Torznab torrent download failed (HTTP %s). Waiting %ss before retry.", status, TORZNAB_COOLDOWN_SECONDS)
            await asyncio.sleep(TORZNAB_COOLDOWN_SECONDS)
        last_error = f"HTTP {status} ({detail})"
    raise RuntimeError(f"Téléchargement .torrent échoué: {last_error}")


def is_av1_title(title: str) -> bool:
    return "av1" in title.lower()
