"""Historical search operations extracted without changing their behavior."""
from modules.plex.config import *
import unicodedata


def language_matches(title, language):
    word = language.lower()
    aliases = {"français": ["french", "multi", "vff", "vfi", "vf"],
               "francais": ["french", "multi", "vff", "vfi", "vf"],
               "fr": ["french", "multi", "vff", "vfi", "vf"],
               "french": ["french", "multi", "vff", "vfi", "vf"],
               "anglais": ["english", "eng", "multi"], "en": ["english", "eng", "multi"]}
    return any(re.search(r"\b" + re.escape(t) + r"\b", title.lower()) for t in aliases.get(word, [word]))


def seed_count(item):
    value = str(item.get("seeders", "")).strip()
    return int(value) if re.fullmatch(r"\d+", value) else None


def title_tokens(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"['’]", "", value)
    return set(re.findall(r"[a-z0-9]+", value))


def select_results(items, query, *, year=None, quality=None, language=None, season=0, episode=0, min_seeders=None):
    """Deterministic natural-search policy; quality is never silently changed."""
    candidates = rank_results(items, query, year=year, season=season, episode=episode,
                              strict_series=True, min_seeders=min_seeders)
    wanted = title_tokens(query)
    candidates = [i for i in candidates if wanted <= title_tokens(i.get("title", ""))
                  and not is_av1_title(i.get("title", ""))
                  and (not year or re.search(r"\b" + str(year) + r"\b", i.get("title", "")))
                  and (not language or language_matches(i.get("title", ""), language))
                  and seed_count(i) != 0]
    options = [q for q in ("2160p", "1080p", "720p", "480p")
               if any(quality_matches(i.get("title", ""), q) for i in candidates)]
    selected = [i for i in candidates if quality_matches(i.get("title", ""), quality)]

    def order(item):
        multi = bool(re.search(r"\bmulti\b", item.get("title", ""), re.I))
        seeds = seed_count(item)
        size = parse_size_bytes(item.get("size", ""))
        return (0 if language or multi else 1, 0 if seeds is not None else 1,
                size if size > 0 else float("inf"), -(seeds or 0), item.get("title", ""))

    selected.sort(key=order)
    for item in selected:
        title = item.get("title", "")
        seeds = seed_count(item)
        size = parse_size_bytes(item.get("size", ""))
        lang = "MULTI" if re.search(r"\bmulti\b", title, re.I) else language or (
            "français repéré" if language_matches(title, "français") else
            "anglais repéré" if language_matches(title, "anglais") else "langue non confirmée")
        weight = f"{size / 1024 ** 3:.2f} Gio" if size > 0 else "taille inconnue"
        item["selection_reason"] = f"{quality or 'qualité non précisée'} · {lang} · {weight} · seeds : {seeds if seeds is not None else 'inconnus'}"
        item["preference_matches"]["quality"] = quality_matches(title, quality) if quality else None
        item["preference_matches"]["language"] = language_matches(title, language) if language else None
    return selected, options


def series_numbers(title):
    found = re.findall(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b|\b(\d{1,2})x(\d{1,3})\b|\bsaison[ ._-]*(\d{1,2})(?:[ ._-]+episode[ ._-]*(\d{1,3}))?\b", title, re.I)
    return [(int(s or s2 or french), int(e or e2 or french_ep or 0)) for s, e, s2, e2, french, french_ep in found]


def rank_results(items, query, *, year=None, quality=None, language=None, season=0, episode=0,
                 strict_series=False, min_seeders=None):
    """Rank broad results, with optional strict season/episode and seed requirements."""
    def tokens(value):
        return set(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()))
    wanted = tokens(query)
    ranked = []
    for original in items:
        item = dict(original)
        title = item.get("title", "")
        score = 30 * len(wanted & tokens(title)) / max(len(wanted), 1)
        years = {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", title)}
        match_year = (year in years if years else None) if year else None
        match_quality = quality_matches(title, quality) if quality else None
        match_language = language_matches(title, language) if language else None
        numbers = series_numbers(title)
        match_season = any(s == season for s, _ in numbers) if season and numbers else None
        match_episode = any(s == season and e == episode for s, e in numbers) if episode and numbers else None
        if strict_series and ((season and match_season is not True) or (episode and match_episode is not True)):
            continue
        if strict_series and season and not episode and not any(s == season and e == 0 for s, e in numbers):
            continue
        seeds = seed_count(item)
        if min_seeders is not None and (seeds is None or seeds < min_seeders):
            continue
        score += 100 if match_year is True else -100 if match_year is False else 0
        score += 15 if match_season is True else -15 if match_season is False else 0
        score += 15 if match_episode is True else -15 if match_episode is False else 0
        score += 10 if match_quality else 0
        score += 10 if match_language else 0
        if strict_series and season and not episode and any(s == season and e == 0 for s, e in numbers):
            score += 15  # Prefer a season pack when the user requested the whole season.
        item["preference_matches"] = {"year": match_year, "quality": match_quality, "language": match_language,
                                      "season": match_season, "episode": match_episode}
        ranked.append((score, seeds or 0, item))
    ranked.sort(key=lambda row: (row[1], row[0]) if min_seeders is not None else (row[0], row[1]), reverse=True)
    return [row[2] for row in ranked]

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
            elif name == "size" and value.isdigit():
                enclosure_length = value
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
