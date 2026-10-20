import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiohttp import web
from chamoxis_common.store import Store
from manager.ai.catalog import TMDb, library_request, identified_intent
from manager.ai.gateway import Gateway
from modules.plex.core.library import Library, LibraryUnavailable
from modules.plex.core.catalog_search import search_indexer
from modules.plex.core.search import select_results, parse_rss_feed
from shared.schemas import Chat, Search


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'state.sqlite3')
        self.calls = []
        async def upstream(request):
            self.calls.append((request.path, dict(request.query), dict(request.headers)))
            if request.path == '/search/movie':
                rows = [{'id': 1, 'title': 'Dune', 'original_title': 'Dune', 'release_date': '1984-01-01'},
                        {'id': 2, 'title': 'Dune', 'original_title': 'Dune', 'release_date': '2021-01-01'}]
                return web.json_response({'results': rows})
            if request.path == '/search/tv':
                return web.json_response({'results': []})
            if request.path == '/movie/2':
                return web.json_response({'id': 2, 'title': 'Dune', 'release_date': '2021-01-01', 'imdb_id': 'tt1160419', 'genres': [{'name': 'Science-fiction'}], 'alternative_titles': {'titles': [{'title': 'Dune: Part One', 'iso_3166_1': 'US'}]}})
            if request.path == '/library/sections':
                return web.json_response({'MediaContainer': {'Directory': [{'key': '2', 'type': 'show'}]}})
            if request.path == '/library/sections/2/all':
                return web.json_response({'MediaContainer': {'Metadata': [{'title': 'The Expanse', 'type': 'show', 'year': 2015, 'rating': 9, 'Genre': [{'tag': 'Science Fiction'}], 'Guid': [{'id': 'imdb://tt3230854'}, {'id': 'tmdb://63639'}]}, {'title': 'Friends', 'type': 'show', 'Genre': [{'tag': 'Comedy'}]}]}})
            return web.Response(status=401)
        app = web.Application()
        app.router.add_get('/{path:.*}', upstream)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.store.close()
        self.tmp.cleanup()

    async def test_ambiguous_title_then_exact_year_and_cached_details(self):
        tmdb = TMDb(self.store, 'test-token', self.url)
        ambiguous = await tmdb.resolve('Dune', 'movies')
        self.assertIsNone(ambiguous['selected'])
        self.assertEqual(len(ambiguous['items']), 2)
        identified = await tmdb.resolve('Dune', 'movies', 2021)
        self.assertEqual(identified['selected']['imdb_id'], 'tt1160419')
        self.assertIn('Dune: Part One', identified['selected']['aliases'])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0][2]['Authorization'], 'Bearer test-token')
        self.assertNotIn('test-token', str(self.calls[0][1]))
        self.assertEqual(identified_intent({'season': 1}, identified['selected'])['season'], 1)
        mismatch = await tmdb.resolve('Dune', 'movies', 2026)
        self.assertIsNone(mismatch['selected'])

    async def test_unconfigured_and_unavailable_metadata_do_not_claim_identification(self):
        self.assertFalse((await TMDb(self.store, '', self.url).resolve('Dune', 'movies'))['configured'])
        result = await TMDb(self.store, 'token', self.url + '/wrong').resolve('Dune', 'movies')
        self.assertEqual(result['items'], [])
        self.assertIn('HTTP 401', result['warning'])

    async def test_series_details_preserve_each_season_first_air_date(self):
        tmdb = TMDb(self.store, 'token')
        tmdb.get = AsyncMock(return_value={'id': 1, 'name': 'Show', 'seasons': [
            {'season_number': 1, 'episode_count': 8, 'air_date': '2022-09-02'},
            {'season_number': 2, 'episode_count': 8, 'air_date': None},
            {'season_number': 0, 'episode_count': 1, 'air_date': None}]})
        details = await tmdb.details('series', 1)
        self.assertEqual([s['air_date'] for s in details['seasons']], ['2022-09-02', None])
        self.assertEqual([s['season'] for s in details['seasons']], [1, 2])

    async def test_real_plex_inventory_genres_ids_cache_and_invalid_section(self):
        library = Library(self.store, self.url, 'plex-test', series='2')
        result = await library.browse('series', 'SF')
        self.assertTrue(result['authoritative'])
        self.assertEqual(result['total'], 2)
        self.assertEqual([i['title'] for i in result['items']], ['The Expanse'])
        self.assertEqual(result['items'][0]['tmdb_id'], 63639)
        self.assertEqual(self.calls[0][2]['X-Plex-Token'], 'plex-test')
        await library.browse('series', query='Friends')
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(LibraryUnavailable):
            await Library(self.store, self.url, 'plex-test', series='99').browse('series', refresh=True)
        with self.assertRaises(LibraryUnavailable):
            await Library(self.store, self.url, '').browse('series')

    async def test_sf_routes_to_library_without_calling_model(self):
        router = SimpleNamespace(generate=AsyncMock())
        gateway = Gateway(router)
        request = 'Qu’est-ce que je peux regarder comme série de SF sur le Plex ?'
        result = await gateway.route(Chat(messages=[{'role': 'user', 'content': request}]))
        self.assertEqual(result['route']['action'], 'library')
        self.assertEqual(await gateway.library(request), {'kind': 'series', 'genre': 'science-fiction', 'query': ''})
        router.generate.assert_not_awaited()
        self.assertFalse(library_request('Comment fonctionne Plex ?'))
        self.assertFalse(library_request('Ajoute Dune sur Plex'))

    async def test_indexer_capabilities_ids_aliases_dedup_and_fallback(self):
        rss = '<rss><channel><item><title>Original.S01.MULTI.1080p.H264</title><link>http://download/1</link></item></channel></rss>'
        async def fetch(url):
            if 't=caps' in url:
                return '<caps><searching><tv-search available="yes" supportedParams="q,imdbid,season,ep"/></searching></caps>'
            if 't=tvsearch' in url:
                raise ConnectionError('ID lookup unsupported despite advertised capability')
            return rss
        engine = SimpleNamespace(build_torznab_search_url=lambda q, limit, i: 'http://tracker/search?t=search&q='+q, fetch_rss=AsyncMock(side_effect=fetch), parse_rss_feed=parse_rss_feed, indexer_label=lambda i:i)
        body = Search(query='Titre français', query_aliases=['Titre français', 'Original'], media_kind='series', imdb_id='tt1234', season=1, selection_policy=True, quality='1080p')
        rows = await search_indexer(engine, self.store, body, '1')
        self.assertEqual(len(rows), 1)
        urls = [c.args[0] for c in engine.fetch_rss.await_args_list]
        self.assertTrue(any('imdbid=1234' in u and 'season=1' in u for u in urls))
        chosen, _ = select_results(rows, body.query, season=1, quality='1080p', query_aliases=body.query_aliases)
        self.assertEqual(len(chosen), 1)
        self.assertIn('temps', chosen[0]['availability_warning'])

    async def test_pagination_finds_release_beyond_first_hundred(self):
        from urllib.parse import parse_qs, urlsplit
        offsets = []
        async def fetch(url):
            params = parse_qs(urlsplit(url).query)
            offset = int(params['offset'][0])
            size = int(params['limit'][0])
            offsets.append((offset, size))
            rows = ''.join(f'<item><title>{"Below.S01.2160p" if i == 220 else "Other"}</title><link>http://download/{i}</link></item>' for i in range(offset, offset+size))
            return '<rss><channel>'+rows+'</channel></rss>'
        engine = SimpleNamespace(build_torznab_search_url=lambda *a: 'http://tracker/?t=search&q=Below', fetch_rss=fetch, parse_rss_feed=parse_rss_feed, indexer_label=lambda i:i)
        rows = await search_indexer(engine, self.store, Search(query='Below'), '3')
        self.assertEqual(offsets, [(0, 100), (100, 100), (200, 50)])
        self.assertEqual(len(rows), 250)
        self.assertTrue(any(i['title'] == 'Below.S01.2160p' for i in rows))

    def test_default_quality_is_preference_but_explicit_quality_is_strict(self):
        items = [{'title': f'Below.2026.S01.MULTI.{quality}.H265', 'seeders': '8', 'size': size} for quality, size in [('2160p', '100'), ('1080p', '200')]]
        preferred, _ = select_results(items, 'Below', season=1, quality='1080p', prefer_quality=True)
        self.assertEqual(len(preferred), 2)
        self.assertIn('1080p', preferred[0]['title'])
        self.assertIn('2160p', preferred[1]['selection_reason'])
        strict, _ = select_results(items, 'Below', season=1, quality='1080p')
        self.assertEqual(len(strict), 1)

    async def test_refined_search_finds_targeted_release_and_deduplicates(self):
        from urllib.parse import parse_qs, urlsplit
        queries = []
        async def fetch(url):
            params = parse_qs(urlsplit(url).query)
            query = params.get('q', [''])[0]
            queries.append(query)
            item = '<item><title>Below.2026.S01.2160p.H265</title><link>http://download/below</link></item>' if query in {'Below S01', 'Below 2026 S01'} else ''
            return '<rss><channel>'+item+'</channel></rss>'
        engine = SimpleNamespace(build_torznab_search_url=lambda q, *a: 'http://tracker/?q='+q, fetch_rss=fetch, parse_rss_feed=parse_rss_feed, indexer_label=lambda i:i)
        rows = await search_indexer(engine, self.store, Search(query='Below', year=2026, season=1, refined=True), '3')
        self.assertEqual(len(rows), 1)
        self.assertIn('Below S01', queries)
        self.assertIn('Below 2026', queries)
        self.assertIn('Below 2026 S01', queries)

    async def test_harry_potter_exact_series_does_not_hide_franchise_movies(self):
        async def get(path, **params):
            if path == '/search/tv':
                return {'results': [{'id': 100, 'name': 'Harry Potter', 'first_air_date': '2027-01-01'}]}
            return {'results': [{'id': i, 'title': 'Harry Potter et ' + name, 'release_date': str(year)+'-01-01'} for i, name, year in [(1, 'la Pierre philosophale', 2001), (2, 'la Chambre des secrets', 2002), (3, 'le Prisonnier d’Azkaban', 2004), (4, 'la Coupe de feu', 2005), (5, 'l’Ordre du Phénix', 2007), (6, 'le Prince de sang-mêlé', 2009), (7, 'les Reliques de la Mort partie 1', 2010), (8, 'les Reliques de la Mort partie 2', 2011)]]}
        tmdb = TMDb(self.store, 'token')
        tmdb.get = AsyncMock(side_effect=get)
        tmdb.details = AsyncMock()
        for guessed_kind in ('series', 'movies'):
            result = await tmdb.resolve('Harry Potter', guessed_kind)
            self.assertIsNone(result['selected'])
            self.assertEqual(len(result['items']), 9)
            self.assertEqual({i['kind'] for i in result['items']}, {'movies', 'series'})
        tmdb.details.assert_not_awaited()
        self.assertEqual({c.args[0] for c in tmdb.get.await_args_list}, {'/search/movie', '/search/tv'})

    async def test_exact_title_year_automatically_selects_only_when_unambiguous(self):
        movie = {'id': 2, 'title': 'Harry Potter et la Chambre des secrets', 'release_date': '2002-01-01'}
        tmdb = TMDb(self.store, 'token')
        tmdb.get = AsyncMock(side_effect=lambda path, **_: {'results': [movie]} if path == '/search/movie' else {'results': [{'id': 100, 'name': 'Harry Potter', 'first_air_date': '2027-01-01'}]})
        tmdb.details = AsyncMock(return_value={'tmdb_id': 2, 'kind': 'movies'})
        result = await tmdb.resolve(movie['title'], 'series', 2002)
        self.assertEqual(result['selected']['tmdb_id'], 2)
        tmdb.details.assert_awaited_once_with('movies', 2)

    async def test_one_failed_category_prevents_automatic_match(self):
        from manager.ai.catalog import CatalogUnavailable
        async def get(path, **_):
            if path == '/search/tv':
                raise CatalogUnavailable('TMDb indisponible pour les séries')
            return {'results': [{'id': 1, 'title': 'Dune', 'release_date': '2021-01-01'}]}
        tmdb = TMDb(self.store, 'token')
        tmdb.get = AsyncMock(side_effect=get)
        result = await tmdb.resolve('Dune', 'movies', 2021)
        self.assertIsNone(result['selected'])
        self.assertEqual(len(result['items']), 1)
        self.assertIn('indisponible', result['warning'])

    async def test_unified_search_always_uses_ids_and_targeted_queries_even_for_old_client(self):
        from urllib.parse import parse_qs, urlsplit
        queries = []
        async def fetch(url):
            params = parse_qs(urlsplit(url).query)
            queries.append(params)
            if params.get('t') == ['caps']:
                return '<caps><searching><tv-search available="yes" supportedParams="q,imdbid,tmdbid,season,ep"/></searching></caps>'
            return '<rss><channel/></rss>'
        engine = SimpleNamespace(build_torznab_search_url=lambda q, *a: 'http://tracker/?t=search&q='+q, fetch_rss=fetch, parse_rss_feed=parse_rss_feed, indexer_label=lambda i:i)
        await search_indexer(engine, self.store, Search(query='Below', year=2026, season=1, imdb_id='tt123', tmdb_id=456, media_kind='series', refined=False), '3')
        self.assertTrue(any(q.get('imdbid') == ['123'] for q in queries))
        self.assertTrue(any(q.get('tmdbid') == ['456'] and q.get('season') == ['1'] for q in queries))
        for query in ('Below', 'Below S01', 'Below 2026', 'Below 2026 S01'):
            self.assertTrue(any(q.get('q') == [query] for q in queries), query)
