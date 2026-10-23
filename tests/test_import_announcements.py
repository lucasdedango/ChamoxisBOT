import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from chamoxis_common.store import Store
from manager.core.notifications import Notifications, import_announcement
from modules.plex.core.downloads import Downloads


class ImportAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db')
        self.env = patch.dict(os.environ, {'ALERT_CHANNEL_ID': '123', 'DISCORD_NOTIFICATION_CHANNEL_IDS': '123'})
        self.env.start()
        self.message = SimpleNamespace(id=987, edit=AsyncMock())
        self.channel = SimpleNamespace(send=AsyncMock(return_value=self.message), fetch_message=AsyncMock(return_value=self.message))
        self.bot = SimpleNamespace(is_ready=lambda: True, get_channel=lambda _: self.channel, fetch_channel=AsyncMock())
        self.worker = Notifications(self.store, self.bot)
        self.downloads = Downloads(self.store, None)
        self.task = self.store.create_task('private', {'private': True, 'user_id': 424242, 'channel_id': None,
            'title': 'Raw.Release.Title', 'prefs': {'kind': 'movies', 'target_name': 'Mon film (2026)'}})

    async def asyncTearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def complete(self, **changes):
        data = {'moved_files': 1, 'refresh': [{'section': '1', 'ok': True}],
                'message': r'Imported from C:\private\downloads to D:\plex\film.mkv', 'reason': 'PRIVATE-DETAIL'}
        self.downloads.emit(self.task, 'plex.import.completed', {**data, **changes})

    async def test_only_successful_final_import_is_public_without_progress_or_private_details(self):
        for kind in ('torrent.added', 'torrent.progress', 'torrent.completed', 'plex.import.started', 'torrent.failed', 'plex.import.failed'):
            self.downloads.emit(self.task, kind, {'name': 'Private release', 'progress': .5})
        self.complete()
        await self.worker.step()
        self.channel.send.assert_awaited_once()
        text = self.channel.send.await_args.args[0]
        self.assertIn('Nouveau film importé pour Plex', text)
        self.assertIn('Mon film (2026)', text)
        self.assertIn('Actualisation Plex demandée', text)
        for value in ('C:', 'D:', 'PRIVATE-DETAIL', '424242', self.task['id'], 'Raw.Release.Title', 'Private release'):
            self.assertNotIn(value, text)
        self.assertEqual(self.store.pending_events(), [])
        self.assertEqual(self.channel.send.await_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_replayed_import_does_not_send_another_announcement_or_overwrite_progress(self):
        self.store.set(f"progress_message:123:{self.task['id']}", 777)
        self.complete()
        await self.worker.step()
        await self.worker.step()
        self.complete()  # Distinct event ID for the same completed task.
        await self.worker.step()
        self.channel.send.assert_awaited_once()
        self.channel.fetch_message.assert_awaited_once_with(987)
        self.message.edit.assert_awaited_once()

    async def test_refresh_failure_is_announced_as_unconfirmed_library_availability(self):
        self.complete(refresh=[{'ok': False, 'reason': 'http://private.example/?token=SECRET'}])
        await self.worker.step()
        text = self.channel.send.await_args.args[0]
        self.assertIn('actualisation de Plex a échoué', text)
        self.assertIn('reste à vérifier', text)
        self.assertNotIn('Actualisation Plex demandée', text)
        self.assertNotIn('SECRET', text)

    async def test_missing_refresh_does_not_claim_plex_scan_succeeded(self):
        self.complete(refresh=[])
        await self.worker.step()
        self.assertIn('Actualisation Plex non confirmée', self.channel.send.await_args.args[0])

    async def test_simulated_or_zero_file_import_does_not_announce_new_content(self):
        self.downloads.emit(self.task, 'torrent.simulated')
        self.complete(moved_files=0)
        await self.worker.step()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.store.pending_events(), [])

    async def test_series_episode_and_season_announcements_do_not_claim_entire_series_added(self):
        for episode, phrase in ((3, 'Nouvel épisode'), (0, 'Nouveaux épisodes')):
            self.task['payload']['prefs'] = {'kind': 'series', 'target_name': 'Ma série', 'season': 2, 'episode': episode}
            self.complete()
            event = self.store.pending_events()[-1]
            text = import_announcement(event)
            self.assertIn(phrase, text)
            self.assertIn('S02E03' if episode else 'saison 2', text)
            self.assertNotIn('série complète', text)

    async def test_missing_or_unapproved_announcement_channel_does_not_leak_to_request_channel(self):
        for channel in ('', '456'):
            with patch.dict(os.environ, {'ALERT_CHANNEL_ID': channel}):
                self.task['payload']['channel_id'] = 123
                self.complete()
                with self.assertLogs('manager.core.notifications', 'WARNING'):
                    await self.worker.step()
        self.channel.send.assert_not_awaited()

    async def test_offline_bot_keeps_completion_until_it_can_be_announced(self):
        self.complete()
        self.bot.is_ready = lambda: False
        await self.worker.step()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events WHERE delivered=0').fetchone()[0], 1)
        self.bot.is_ready = lambda: True
        self.store.db.execute('UPDATE events SET next_attempt=0')
        self.store.db.commit()
        await self.worker.step()
        self.channel.send.assert_awaited_once()
