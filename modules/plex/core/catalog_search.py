"""Try Torznab identifiers and title variants, stopping after satisfactory results."""
import time
import logging

logger = logging.getLogger(__name__)
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from xml.etree import ElementTree as ET
from modules.plex.core.search import seed_count, rejection_reasons, normalize_search_text


def satisfactory_results(items, body):
    """Do not stop on unrelated results, AV1 or availability/quality fallbacks."""
    return any((seed_count(item) or 0) >= max(1, body.min_seeders or 1) and not rejection_reasons(
        item, body.query, query_aliases=body.query_aliases, year=body.year, quality=body.quality,
        language=body.language, season=body.season, episode=body.episode,
        media_kind=body.media_kind if body.selection_policy else None) for item in items)


def parameters(url, **changes):
    parts = urlsplit(url)
    values = dict(parse_qsl(parts.query))
    values.update(changes)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(values), ''))


async def search_indexer(engine, store, body, indexer):
    base = engine.build_torznab_search_url(body.query, body.limit, indexer)
    urls = []
    supported = []
    if body.imdb_id or body.tmdb_id:
        category = 'tv-search' if body.media_kind == 'series' else 'movie-search'
        key = 'torznab_caps:' + str(indexer)
        cache = store.get(key)
        capabilities = cache['data'] if cache and cache['expires'] > time.time() else None
        if capabilities is None:
            capabilities = {}
            try:
                root = ET.fromstring(await engine.fetch_rss(parameters(base, t='caps', q='')))
                for node in root.iter():
                    name = node.tag.rsplit('}', 1)[-1]
                    if name in {'tv-search', 'movie-search'} and node.get('available', '').lower() == 'yes':
                        capabilities[name] = [p.strip() for p in node.get('supportedParams', '').split(',')]
            except Exception:
                pass  # Metadata support is optional, ordinary searches remain available.
            store.set(key, {'data': capabilities, 'expires': time.time() + (86400 if capabilities else 300)})
        supported = capabilities.get(category, [])
        for field, value in (('imdbid', body.imdb_id[2:] if body.imdb_id else None), ('tmdbid', body.tmdb_id)):
            if not value or field not in supported:
                continue
            params = {'t': 'tvsearch' if category == 'tv-search' else 'movie', 'q': '', field: str(value)}
            if body.season and 'season' in supported:
                params['season'] = body.season
            if body.episode and 'ep' in supported:
                params['ep'] = body.episode
            urls.append(parameters(base, **params))
    normalized = {}
    for title in [body.query, *body.query_aliases]:
        title = normalize_search_text(title)
        if title:
            normalized.setdefault(title.casefold(), title)
    titles = list(normalized.values())[:4]
    targeted = []
    for title in titles:
        season = f"S{body.season:02d}" + (f"E{body.episode:02d}" if body.episode else '') if body.season else ''
        suffix = season or (str(body.year) if body.year else '')
        if suffix:
            targeted.append(title + ' ' + suffix)
        targeted.append(title)
        # Some trackers match release-name punctuation differently from prose.
        targeted.append(title.replace(' ', '.'))
    # Additional targeted variants are a last resort after all main languages.
    for title in titles:
        season = f"S{body.season:02d}" + (f"E{body.episode:02d}" if body.episode else '') if body.season else ''
        for suffix in (str(body.year) if body.year else '', ' '.join(str(v) for v in (body.year, season) if v)):
            if suffix:
                targeted.append(title + ' ' + suffix)
    titles = list(dict.fromkeys(targeted))
    urls.extend(engine.build_torznab_search_url(title, body.limit, indexer) for title in titles)
    urls = list(dict.fromkeys(urls))[:12]
    rows, successes = {}, 0
    for base_url in urls:
        seen = set()
        for offset in (0, 100, 200):
            page_size = min(100, 250 - offset)
            url = parameters(base_url, limit=str(page_size), offset=str(offset))
            try:
                xml = await engine.fetch_rss(url)
                items = engine.parse_rss_feed(xml, page_size, engine.indexer_label(indexer))
                successes += 1
                logger.info("Indexer page indexer=%s offset=%s requested=%s received=%s refined=%s", indexer, offset, page_size, len(items), True)
                fresh = False
                for item in items:
                    ref = item.get('enclosure') or item.get('link') or item.get('title')
                    fresh |= ref not in seen
                    seen.add(ref)
                    previous = rows.get(ref)
                    if previous is None or (seed_count(item) or 0) > (seed_count(previous) or 0):
                        rows[ref] = item
                if len(items) < page_size or not fresh:
                    break  # Finished, or indexer ignored the requested offset.
            except Exception:
                break  # Other title/identifier searches can still succeed.
        if satisfactory_results(rows.values(), body):
            logger.info('Indexer search stopped indexer=%s reason=compatible_non_av1_with_seeds queries=%s', indexer, urls.index(base_url) + 1)
            break
    if not successes:
        raise RuntimeError('Indexer searches failed')
    return list(rows.values())
