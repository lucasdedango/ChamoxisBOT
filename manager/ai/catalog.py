"""TMDb metadata: fixed upstream, bounded requests, persistent cache, no actions."""
import logging
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


logger = logging.getLogger(__name__)


class CatalogUnavailable(RuntimeError):
    pass


class TMDb:
    def __init__(self, store, token=None, base_url='https://api.themoviedb.org/3'):
        self.store = store
        self.token = os.getenv('TMDB_ACCESS_TOKEN', '') if token is None else token
        self.language = os.getenv('TMDB_LANGUAGE', 'fr-FR')
        if not self.token:
            logger.warning('TMDb identification disabled: TMDB_ACCESS_TOKEN is missing from the running manager; env_file=%s', os.getenv('MANAGER_ENV_FILE', '.env'))
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
        results = await asyncio.gather(
            self.get('/search/movie', query=query, include_adult='false'),
            self.get('/search/tv', query=query, include_adult='false'), return_exceptions=True)
        rows, failures, seen = [], [], set()
        for category, data in zip(('movies', 'series'), results):
            if isinstance(data, Exception):
                failures.append(str(data) if isinstance(data, CatalogUnavailable) else 'TMDb indisponible')
                continue
            for item in data.get('results', []):
                if not item.get('id') or (category, item['id']) in seen:
                    continue
                seen.add((category, item['id']))
                rows.append(self.public(item, category))

        def words(value):
            return {normalize(w) for w in re.findall(r"[^\W_]+", value.casefold())}

        wanted = words(query)
        exact = lambda row: normalize(query) in {normalize(row['title']), normalize(row['original_title'])}
        plausible = [r for r in rows if exact(r) or any(wanted and wanted <= words(t) for t in (r['title'], r['original_title']))]
        choices = plausible or rows
        matching_year = [r for r in choices if r['year'] == year] if year else []
        if matching_year:
            choices = matching_year
        choices.sort(key=lambda r: (not exact(r), r['kind'] != kind, r['title'].casefold(), r['year'] or 9999))
        # Evaluate ambiguity BEFORE limiting display. Never auto-pick if a category failed.
        automatic = len(choices) == 1 and exact(choices[0]) and not failures and (not year or choices[0]['year'] == year)
        warning = '; '.join(dict.fromkeys(failures)) or None
        if year and choices and not matching_year:
            warning = (warning + '; ' if warning else '') + 'Aucune fiche ne correspond à l’année demandée; précise ou choisis une autre œuvre'
        selected = None
        if automatic:
            try:
                selected = await self.details(choices[0]['kind'], choices[0]['tmdb_id'])
            except CatalogUnavailable as error:
                warning = str(error)
        if not choices and not warning:
            warning = 'Aucune fiche TMDb trouvée; recherche par titre conservée'
        return {'configured': True, 'items': choices[:40], 'selected': selected,
                'truncated': len(choices) > 40, 'warning': warning}

    async def details(self, kind, media_id):
        category = 'tv' if kind == 'series' else 'movie'
        item = await self.get(f'/{category}/{media_id}', append_to_response='external_ids,alternative_titles')
        result = self.public(item, kind)
        imdb = item.get('imdb_id') or item.get('external_ids', {}).get('imdb_id')
        result['imdb_id'] = imdb if isinstance(imdb, str) and re.fullmatch(r'tt\d+', imdb) else None
        result['genres'] = [g['name'] for g in item.get('genres', []) if g.get('name')]
        result['seasons'] = [{'season': s['season_number'], 'episodes': s.get('episode_count'), 'name': s.get('name', ''),
                             'air_date': s.get('air_date')}
                             for s in item.get('seasons', []) if s.get('season_number', 0) > 0]
        alternatives = item.get('alternative_titles', {})
        aliases = [result['title'], result['original_title']] + [a.get('title', '') for a in alternatives.get('results', alternatives.get('titles', [])) if a.get('iso_3166_1') in {'FR', 'US', 'GB'}]
        result['aliases'] = list(dict.fromkeys(a for a in aliases if a))[:4]
        return result


def identified_intent(intent, media):
    return {**intent, 'title': media['title'], 'kind': media['kind'], 'year': media.get('year'),
            'imdb_id': media.get('imdb_id'), 'tmdb_id': media['tmdb_id'], 'query_aliases': media.get('aliases', []),
            'media': media, 'clarification': None}
