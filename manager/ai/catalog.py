"""TMDb metadata: fixed upstream, bounded requests, persistent cache, no actions."""
import asyncio
import os
import re
import time
import unicodedata
import aiohttp


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text).casefold() if c.isalnum() and not unicodedata.combining(c))


def library_request(text):
    value = normalize(text)
    return ('plex' in value or 'bibliotheque' in value) and any(word in value for word in ('regarder', 'disponible', 'recommande', 'conseille', 'present', 'dessus', 'surplex', 'surleplex', 'danslabibliotheque')) and not any(word in value for word in ('telecharge', 'ajoute', 'torrent', 'supprime'))


def library_filters(text):
    value = normalize(text)
    kind = 'series' if any(word in value for word in ('serie', 'series')) else 'movies' if 'film' in value else 'all'
    science = bool(re.search(r'\bsf\b|sci[ -]?fi|science[ -]?fiction', text, re.I))
    return {'kind': kind, 'genre': 'science-fiction' if science else '', 'query': ''}


class CatalogUnavailable(RuntimeError):
    pass


class TMDb:
    def __init__(self, store, token=None, base_url='https://api.themoviedb.org/3'):
        self.store = store
        self.token = os.getenv('TMDB_ACCESS_TOKEN', '') if token is None else token
        self.language = os.getenv('TMDB_LANGUAGE', 'fr-FR')
        self.base_url = base_url.rstrip('/')
        self.lock = asyncio.Semaphore(3)

    async def get(self, path, **params):
        if not self.token:
            raise CatalogUnavailable('TMDb non configuré : renseigner TMDB_ACCESS_TOKEN dans manager/.env')
        key = 'tmdb:' + path + ':' + str(sorted(params.items())) + ':' + self.language
        cached = self.store.get(key)
        if cached and cached['expires'] > time.time():
            return cached['data']
        try:
            async with self.lock, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
                async with client.get(self.base_url + path, params={'language': self.language, **params},
                                      headers={'Authorization': 'Bearer ' + self.token}, allow_redirects=False) as response:
                    if response.status != 200:
                        raise CatalogUnavailable('TMDb a refusé la requête (HTTP ' + str(response.status) + ')')
                    data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise CatalogUnavailable('TMDb indisponible; réessaie plus tard') from error
        self.store.set(key, {'expires': time.time() + 86400, 'data': data})
        return data

    @staticmethod
    def public(item, kind):
        title = item.get('title') or item.get('name') or ''
        date = item.get('release_date') or item.get('first_air_date') or ''
        year = int(date[:4]) if re.match(r'^\d{4}', date) else None
        poster = item.get('poster_path') or ''
        return {'tmdb_id': int(item['id']), 'kind': kind, 'title': title,
                'original_title': item.get('original_title') or item.get('original_name') or title,
                'year': year, 'overview': (item.get('overview') or '')[:1500],
                'poster_url': 'https://image.tmdb.org/t/p/w342' + poster if poster.startswith('/') else None,
                'tmdb_url': f"https://www.themoviedb.org/{'tv' if kind == 'series' else 'movie'}/{int(item['id'])}"}

    async def resolve(self, query, kind, year=None):
        if not self.token:
            return {'configured': False, 'items': [], 'warning': 'TMDb non configuré; recherche par titre conservée'}
        try:
            data = await self.get('/search/' + ('tv' if kind == 'series' else 'movie'), query=query, include_adult='false')
            rows = [self.public(item, kind) for item in data.get('results', []) if item.get('id')]
            if not rows:
                other = 'movies' if kind == 'series' else 'series'
                data = await self.get('/search/' + ('tv' if other == 'series' else 'movie'), query=query, include_adult='false')
                rows = [self.public(item, other) for item in data.get('results', []) if item.get('id')]
            exact = [r for r in rows if normalize(query) in {normalize(r['title']), normalize(r['original_title'])}]
            matching_year = [r for r in rows if r['year'] == year] if year else []
            choices = [r for r in exact if not year or r['year'] == year] or matching_year or exact or rows
            choices = choices[:5]
            automatic = len(choices) == 1 and choices[0] in exact and (not year or choices[0]['year'] == year)
            selected = await self.details(choices[0]['kind'], choices[0]['tmdb_id']) if automatic else None
            return {'configured': True, 'items': choices, 'selected': selected,
                    'warning': None if choices else 'Aucune fiche TMDb trouvée; recherche par titre conservée'}
        except CatalogUnavailable as error:
            return {'configured': True, 'items': [], 'warning': str(error)}

    async def details(self, kind, media_id):
        category = 'tv' if kind == 'series' else 'movie'
        item = await self.get(f'/{category}/{media_id}', append_to_response='external_ids,alternative_titles')
        result = self.public(item, kind)
        imdb = item.get('imdb_id') or item.get('external_ids', {}).get('imdb_id')
        result['imdb_id'] = imdb if isinstance(imdb, str) and re.fullmatch(r'tt\d+', imdb) else None
        result['genres'] = [g['name'] for g in item.get('genres', []) if g.get('name')]
        result['seasons'] = [{'season': s['season_number'], 'episodes': s.get('episode_count'), 'name': s.get('name', '')}
                             for s in item.get('seasons', []) if s.get('season_number', 0) > 0]
        alternatives = item.get('alternative_titles', {})
        aliases = [result['title'], result['original_title']] + [a.get('title', '') for a in alternatives.get('results', alternatives.get('titles', [])) if a.get('iso_3166_1') in {'FR', 'US', 'GB'}]
        result['aliases'] = list(dict.fromkeys(a for a in aliases if a))[:4]
        return result


def identified_intent(intent, media):
    return {**intent, 'title': media['title'], 'kind': media['kind'], 'year': media.get('year'),
            'imdb_id': media.get('imdb_id'), 'tmdb_id': media['tmdb_id'], 'query_aliases': media.get('aliases', []),
            'media': media, 'clarification': None}
