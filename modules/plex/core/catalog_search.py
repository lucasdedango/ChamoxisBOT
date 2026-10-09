"""Use advertised Torznab identifiers, then broad French/original title fallbacks."""
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from xml.etree import ElementTree as ET
from modules.plex.core.search import seed_count


def parameters(url, **changes):
    parts = urlsplit(url)
    values = dict(parse_qsl(parts.query))
    values.update(changes)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(values), ''))


async def search_indexer(engine, store, body, indexer):
    base = engine.build_torznab_search_url(body.query, body.limit, indexer)
    urls = []
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
        identity = {'imdbid': body.imdb_id[2:]} if body.imdb_id and 'imdbid' in supported else {'tmdbid': body.tmdb_id} if body.tmdb_id and 'tmdbid' in supported else {}
        if identity:
            params = {'t': 'tvsearch' if category == 'tv-search' else 'movie', 'q': '', **identity}
            if body.season and 'season' in supported:
                params['season'] = body.season
            if body.episode and 'ep' in supported:
                params['ep'] = body.episode
            urls.append(parameters(base, **params))
    titles = list(dict.fromkeys(q.strip() for q in [body.query, *body.query_aliases] if q.strip()))[:2]
    urls.extend(engine.build_torznab_search_url(title, body.limit, indexer) for title in titles)
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
    if not successes:
        raise RuntimeError('Indexer searches failed')
    return list(rows.values())
