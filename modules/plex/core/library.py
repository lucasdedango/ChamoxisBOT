"""Read-only Plex inventory. A local directory is never treated as a Plex item."""
import asyncio
import re
import time
import unicodedata
import aiohttp


class LibraryUnavailable(RuntimeError):
    pass


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum() and not unicodedata.combining(c))


def genre_matches(genres, query):
    wanted = normalized(query)
    if not wanted:
        return True
    if wanted in {'sf', 'scifi', 'sciencefiction'}:
        return any('sciencefiction' in normalized(g) or 'scifi' in normalized(g) for g in genres)
    return any(wanted in normalized(g) for g in genres)


class Library:
    def __init__(self, store, url, token, movies='', series=''):
        self.store, self.url, self.token = store, (url or '').rstrip('/'), token or ''
        self.sections = {'movies': movies, 'series': series}
        self.lock = asyncio.Lock()

    async def request(self, client, path, **params):
        try:
            async with client.get(self.url + path, params=params,
                                  headers={'X-Plex-Token': self.token, 'Accept': 'application/json'}, allow_redirects=False) as response:
                if response.status != 200:
                    raise LibraryUnavailable('Plex a refusé la consultation (HTTP ' + str(response.status) + '); vérifier PLEX_URL et PLEX_TOKEN')
                return (await response.json())['MediaContainer']
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as error:
            raise LibraryUnavailable('Bibliothèque Plex indisponible; aucune disponibilité ne peut être confirmée') from error

    async def inventory(self, kind='all', refresh=False):
        if not self.url or not self.token:
            raise LibraryUnavailable('Configurer PLEX_URL et PLEX_TOKEN dans modules/plex/.env pour consulter la bibliothèque')
        key = 'plex_inventory:' + kind
        async with self.lock:
            cached = self.store.get(key)
            if cached and cached['expires'] > time.time() and not refresh:
                return cached['data']
            rows, truncated = [], False
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
                sections = (await self.request(client, '/library/sections')).get('Directory', [])
                chosen = [s for s in sections if s.get('type') in {'movie', 'show'} and
                          (kind == 'all' or s.get('type') == ('show' if kind == 'series' else 'movie'))]
                for media_kind, section_id in self.sections.items():
                    if section_id and kind in {'all', media_kind} and not any(str(s.get('key')) == str(section_id) and s.get('type') == ('show' if media_kind == 'series' else 'movie') for s in chosen):
                        raise LibraryUnavailable('Identifiant de section Plex introuvable pour ' + media_kind)
                for section in chosen:
                    media_kind = 'series' if section['type'] == 'show' else 'movies'
                    if self.sections[media_kind] and str(section['key']) != str(self.sections[media_kind]):
                        continue
                    if not str(section['key']).isdigit():
                        continue
                    for page in range(10):
                        data = await self.request(client, '/library/sections/' + str(section['key']) + '/all',
                                                  **{'X-Plex-Container-Start': page * 200, 'X-Plex-Container-Size': 200, 'includeGuids': '1'})
                        items = data.get('Metadata', [])
                        for item in items:
                            if item.get('type') not in {'movie', 'show'}:
                                continue
                            guids = [g.get('id', '') for g in item.get('Guid', [])] + [item.get('guid', '')]
                            imdb = next((m[1] for guid in guids if (m := re.search(r'imdb://(tt\d+)', guid))), None)
                            tmdb = next((int(m[1]) for guid in guids if (m := re.search(r'(?:tmdb://|themoviedb://)(\d+)', guid))), None)
                            rows.append({'title': item.get('title', ''), 'year': item.get('year'), 'kind': media_kind,
                                'summary': (item.get('summary') or '')[:1200], 'genres': [g['tag'] for g in item.get('Genre', []) if g.get('tag')],
                                'rating': item.get('rating') or item.get('audienceRating'), 'rating_key': str(item.get('ratingKey', '')),
                                'tmdb_id': tmdb, 'imdb_id': imdb})
                        if not items or len(items) < 200 or page * 200 + len(items) >= int(data.get('totalSize', 10**9)):
                            break
                    else:
                        truncated = True
            result = {'items': rows, 'authoritative': True, 'truncated': truncated, 'checked_at': time.time()}
            self.store.set(key, {'data': result, 'expires': time.time() + 120})
            return result

    async def browse(self, kind='all', genre='', query='', limit=20, refresh=False):
        data = await self.inventory(kind, refresh)
        matches = [i for i in data['items'] if genre_matches(i['genres'], genre) and
                   (not query or normalized(query) in normalized(i['title']))]
        matches.sort(key=lambda item: (-(float(item.get('rating') or 0)), item['title'].casefold()))
        return {**data, 'items': matches[:limit], 'matched': len(matches), 'total': len(data['items'])}
