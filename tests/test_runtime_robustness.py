"""Regression checks for slow disk moves, filtering diagnostics and Discord expiry."""
import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote
import discord
import httpx
from chamoxis_common.logging import JSONFormatter, redact
from chamoxis_common.store import Store
from manager.discord import bot as ui
from modules.plex.api.server import create_app
from modules.plex.core.downloads import Downloads
from modules.plex.core.search import is_av1_title, rejection_reasons

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault('PLEX_MOVIES_PATHS', _tmp.name)
os.environ.setdefault('PLEX_SERIES_PATHS', _tmp.name)
from modules.plex.core import engine


class SlowMoveTests(unittest.IsolatedAsyncioTestCase):
    async def move(self, states, destination='/library/Show', timeout=1800):
        qbit = AsyncMock()
        qbit.get_torrent_by_hash.side_effect = states
        clock = [0]
        async def sleep(_):
            clock[0] += 600  # Simulate ten minutes of disk I/O per poll, without waiting.
        with patch.object(engine, 'qbit', qbit), patch.object(engine.asyncio, 'get_running_loop', return_value=SimpleNamespace(time=lambda: clock[0])), patch.object(engine.asyncio, 'sleep', side_effect=sleep):
            result = await engine.move_torrent_and_wait('abc', Path(destination), timeout=timeout)
        return result, qbit

    async def test_copy_can_exceed_idle_timeout_while_moving(self):
        result, qbit = await self.move([{'state': 'moving', 'save_path': '/old'}] * 5 + [{'state': 'stalledUP', 'save_path': '/library/Show'}], timeout=300)
        self.assertEqual(result['state'], 'stalledUP')
        qbit.set_location.assert_awaited_once()
        qbit.list_files.assert_awaited_once_with('abc')

    async def test_destination_path_is_not_completion_while_still_moving(self):
        result, qbit = await self.move([{'state': 'moving', 'save_path': '/library/Show'}, {'state': 'uploading', 'save_path': '/library/Show'}])
        self.assertEqual(qbit.get_torrent_by_hash.await_count, 2)
        self.assertEqual(result['state'], 'uploading')

    async def test_transient_connection_failure_does_not_repeat_set_location(self):
        _, qbit = await self.move([{'state': 'moving'}, ConnectionError('offline'), {'state': 'uploading', 'save_path': '/library/Show'}])
        qbit.set_location.assert_awaited_once()

    async def test_idle_move_times_out_without_logging_entire_private_snapshot(self):
        with self.assertRaises(TimeoutError) as error:
            await self.move([{'state': 'uploading', 'save_path': '/old', 'magnet_uri': 'PRIVATE-TRACKER'}] * 2, timeout=300)
        self.assertNotIn('PRIVATE-TRACKER', str(error.exception))
        self.assertIn('sans état moving', str(error.exception))

    async def test_qbit_disk_error_is_reported_without_waiting(self):
        with self.assertRaisesRegex(RuntimeError, 'Erreur qBittorrent'):
            await self.move([{'state': 'error', 'save_path': '/old'}])


class WorkerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_import_allows_new_downloads_and_shutdown_is_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'state.sqlite3')
            payload = {'link': 'magnet:?xt=example', 'title': 'Show', 'user_id': 42, 'prefs': {'kind': 'series'}}
            first = store.create_task('first', payload)
            store.transition(first['id'], 'downloading', {'hash': 'first'})
            started = asyncio.Event()
            cancelled = asyncio.Event()
            async def import_forever(*args):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            qbit = SimpleNamespace(get_torrent_by_hash=AsyncMock(return_value={'progress': 1, 'state': 'uploading'}), list_torrents=AsyncMock(side_effect=[[], [{'hash': 'second', 'name': 'Show'}]]))
            fake = SimpleNamespace(qbit=qbit, import_torrent_entry=import_forever, DISABLE_TORRENT_DOWNLOAD=False,
                TORZNAB_FORCE_UPLOAD=False, hashes_from_torrents=lambda rows: {t['hash'] for t in rows}, parse_info_hash_from_magnet=lambda _: 'second', add_torrent_from_link=AsyncMock())
            worker = Downloads(store, fake, poll_interval=.005)
            job = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(started.wait(), 1)
                second = store.create_task('second', payload)
                for _ in range(100):
                    if store.task(second['id'])['state'] == 'downloading':
                        break
                    await asyncio.sleep(.005)
                self.assertEqual(store.task(second['id'])['state'], 'downloading')
                self.assertEqual(store.task(first['id'])['state'], 'importing')
                fake.add_torrent_from_link.assert_awaited_once()
            finally:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(store.task(first['id'])['state'], 'importing')
            worker.recover()
            self.assertEqual(store.task(first['id'])['state'], 'needs_review')
            store.close()


class TrackerRedactionTests(unittest.TestCase):
    def test_plain_and_encoded_tracker_passkeys_are_masked_in_tracebacks(self):
        secret = 'testpasskey01234567890123456789'
        url = 'https://tracker.example/announce/' + secret
        urls = [url, quote(url, safe=''), quote(quote(url, safe=''), safe=''), 'https://tracker.example/' + secret + '/announce', 'https://tracker.example/announce?passkey=' + secret]
        for value in urls:
            with self.subTest(value=value):
                try:
                    raise RuntimeError('snapshot magnet:?tr=' + value)
                except RuntimeError:
                    import sys
                    record = logging.LogRecord('test', logging.ERROR, '', 1, 'Import failed', (), sys.exc_info())
                    output = JSONFormatter().format(record)
                self.assertNotIn(secret, output)
                self.assertIn('tracker.example' if value == url else '[redacted]', output)
                self.assertEqual(json.loads(output)['level'], 'ERROR')
        self.assertEqual(redact('https://tracker.example/announce'), 'https://tracker.example/announce')


class SearchDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_explains_filters_and_never_exposes_download_urls(self):
        titles = ['Below.2026.S01.MULTI.1080p.AV1', 'Below.2025.S01.MULTI.1080p.H265', 'Below.2026.S02.MULTI.1080p.H265', 'Below.2026.S01.MULTI.720p.H265', 'Other.2026.S01.MULTI.1080p.H265']
        items = [{'title': title, 'source': 'Tracker', 'link': f'https://download.example/?apikey=PRIVATE-{n}'} for n, title in enumerate(titles)]
        fake = SimpleNamespace(INDEXER_IDS=['3'], build_torznab_search_url=lambda *a: 'search', fetch_rss=AsyncMock(return_value='xml'), parse_rss_feed=lambda *a: items, indexer_label=lambda _: 'Tracker', is_av1_title=is_av1_title)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'PLEX_MODULE_API_KEY': 'test-key'}):
            store = Store(Path(folder) / 'state.sqlite3')
            try:
                app = create_app(fake, store, background=False)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    with self.assertLogs('modules.plex.api.server', 'INFO') as logs:
                        response = await client.post('/search', headers={'X-API-Key': 'test-key'}, json={'query': 'Below', 'year': 2026, 'season': 1, 'quality': '1080p', 'selection_policy': True})
                result = response.json()
                self.assertEqual(result['items'], [])
                for reason in ('av1', 'year', 'season', 'quality', 'title'):
                    self.assertGreater(result['excluded_counts'][reason], 0)
                self.assertNotIn('PRIVATE-', '\n'.join(logs.output) + response.text)
                self.assertIn('Below.2025', '\n'.join(logs.output))
            finally:
                store.close()

    def test_missing_year_is_warning_not_a_false_year_exclusion(self):
        self.assertEqual(rejection_reasons({'title': 'Below.S01.MULTI.1080p.H265'}, 'Below', year=2026, season=1, quality='1080p'), [])


class DiscordAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_search_acknowledges_before_analysis(self):
        events = []
        fake = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(defer=AsyncMock(side_effect=lambda **_: events.append('ack')), send_message=AsyncMock()), edit_original_response=AsyncMock())
        with patch.dict(os.environ, {'DISCORD_ALLOWED_USER_IDS': '42'}), patch('manager.discord.private_search.PrivateSearch.begin', AsyncMock(side_effect=lambda _: events.append('analysis'))):
            await ui.recherchetorrent.callback(fake, 'Below S01')
        self.assertEqual(events, ['ack', 'analysis'])
        fake.response.send_message.assert_not_awaited()
        fake.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_expired_interaction_is_not_retried(self):
        fake = SimpleNamespace(id=123, command=SimpleNamespace(qualified_name='recherchetorrent'), response=SimpleNamespace(send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        error = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), {'code': 10062, 'message': 'Unknown interaction'})
        with self.assertLogs('chamoxisbot.discord', 'WARNING') as logs:
            await ui.on_app_command_error(fake, error)
        fake.response.send_message.assert_not_awaited()
        fake.followup.send.assert_not_awaited()
        self.assertEqual(len(logs.output), 1)
