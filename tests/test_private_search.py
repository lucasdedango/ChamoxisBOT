import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from chamoxis_common.store import Store
from manager.discord.conversation import Conversation
from manager.discord.private_search import PrivateSearch, SearchInvite
from manager.discord.commands.plex import demande
from manager.core.notifications import Notifications
from modules.plex.core.downloads import Downloads
from shared.schemas import Event


def interaction(user=42):
    message = SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(user=SimpleNamespace(id=user), channel_id=123, id=9000,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(return_value=message))


class PrivateSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'DISCORD_ALLOWED_USER_IDS': '42', 'DISCORD_ADMIN_IDS': '', 'DISCORD_CONVERSATION_CHANNEL_IDS': '123'})
        self.env.start()
        self.intent = {'title': 'Show', 'kind': 'series', 'year': None, 'quality': '1080p', 'language': None, 'season': 1, 'episode': 0, 'clarification': None}
        self.catalog = {'items': [], 'warning': 'TMDb non configuré'}
        self.choices = [{'title': f'Film {i}', 'kind': 'movies', 'year': 2000+i, 'tmdb_id': i+1} for i in range(40)]
        self.items = [{'title': f'Show.S01.MULTI.1080p.Release{i}', 'enclosure': f'result:{i}', 'seeders': '4'} for i in range(24)]
        self.result = {'items': self.items, 'errors': []}
        async def manager(method, path, **kwargs):
            if path == '/ai/analyze': return {'intent': self.intent}
            if path == '/catalog/resolve': return self.catalog
            if path.startswith('/catalog/media/'):
                chosen = self.choices[int(path.rsplit('/', 1)[1])-1]
                return {**chosen, 'aliases': [chosen['title']], 'tmdb_url': 'https://www.themoviedb.org/movie/'+str(chosen['tmdb_id'])}
            raise AssertionError(path)
        async def plex(method, path, **kwargs):
            if path == '/search': return self.result
            if path == '/downloads': return {'id': 'private-task'}
            raise AssertionError(path)
        self.manager = SimpleNamespace(request=AsyncMock(side_effect=manager))
        self.plex = SimpleNamespace(request=AsyncMock(side_effect=plex))
        self.now = 0
        self.session = PrivateSearch(42, 'Show saison 1 en 1080p', lambda: self.manager, lambda: self.plex, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.env.stop()

    def adds(self):
        return [c for c in self.plex.request.await_args_list if c.args[1] == '/downloads']

    def button(self, name):
        return next(c for c in self.session.view.children if getattr(c, 'label', None) == name)

    async def test_public_message_only_offers_owner_private_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            try:
                conversation = Conversation(store, lambda: self.manager, lambda: self.plex)
                channel = SimpleNamespace(id=123, send=AsyncMock())
                message = SimpleNamespace(id=123, guild=SimpleNamespace(id=1), channel=channel, author=SimpleNamespace(id=42, bot=False), content='man cherche Harry Potter')
                await conversation.handle(message)
                self.manager.request.assert_not_awaited()
                self.plex.request.assert_not_awaited()
                invitation = channel.send.await_args.kwargs['view']
                self.assertIsInstance(invitation, SearchInvite)
                self.assertNotIn('Harry', channel.send.await_args.args[0])
                outsider = interaction(99)
                await invitation.open.callback(outsider)
                self.manager.request.assert_not_awaited()
                owner = interaction()
                await invitation.open.callback(owner)
                self.assertTrue(owner.response.defer.await_args.kwargs['ephemeral'])
                self.assertTrue(invitation.opened)
                self.assertEqual(channel.send.await_count, 1)  # Results never go to the public channel.
                message.content = 'oui'
                await conversation.handle(message)
                self.assertEqual(self.adds(), [])
                calls = self.manager.request.await_count
                await invitation.open.callback(interaction())
                self.assertEqual(self.manager.request.await_count, calls)
            finally:
                store.close()

    async def test_slash_request_is_ephemeral_from_the_start(self):
        fake = interaction()
        with patch('manager.discord.commands.plex.manager_client', return_value=self.manager), patch('manager.discord.commands.plex.plex_client', return_value=self.plex):
            await demande(fake, 'Show S01')
        fake.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertIsNotNone(fake.edit_original_response.await_args.kwargs['view'])
        self.assertEqual(self.adds(), [])

    async def test_twenty_choices_per_page_and_selection_is_not_confirmation(self):
        self.catalog = {'items': self.choices}
        await self.session.start(interaction())
        self.assertEqual(len(self.session.view.select.options), 20)
        self.assertEqual(len(self.session.embed().description.splitlines()), 20)
        self.assertIn('Page 1/2', self.session.embed().footer.text)
        await self.button('Suivant').callback(interaction())
        self.assertEqual(self.session.view.select.options[0].value, '20')
        self.assertEqual(len(self.session.view.select.options), 20)
        self.session.view.select._values = ['21']
        await self.session.view.select.callback(interaction())
        self.assertEqual(self.manager.request.await_args.args[1], '/catalog/media/movies/22')
        self.assertEqual(self.session.stage, 'torrent')
        self.assertEqual(self.adds(), [])
        self.session.view.select._values = ['1']
        await self.session.view.select.callback(interaction())
        self.assertEqual(self.adds(), [])
        await self.button('Confirmer l’ajout').callback(interaction())
        body = self.adds()[0].kwargs['json']
        self.assertTrue(body['confirmed'])
        self.assertTrue(body['private'])
        self.assertIsNone(body['channel_id'])
        self.assertEqual(body['link'], 'result:1')
        self.assertEqual(body['user_id'], 42)
        self.assertEqual(self.session.stage, 'done')

    async def test_old_buttons_other_users_expiry_and_revoked_permissions_cannot_add(self):
        await self.session.start(interaction())
        old = self.button('Confirmer l’ajout')
        self.session.view.select._values = ['1']
        await self.session.view.select.callback(interaction())
        await old.callback(interaction())
        self.assertEqual(self.adds(), [])
        await self.button('Confirmer l’ajout').callback(interaction(99))
        self.assertEqual(self.adds(), [])
        with patch.dict(os.environ, {'DISCORD_ALLOWED_USER_IDS': '99'}):
            await self.button('Confirmer l’ajout').callback(interaction())
        self.assertEqual(self.adds(), [])
        self.now = 301
        await self.button('Confirmer l’ajout').callback(interaction())
        self.assertEqual(self.adds(), [])

    async def test_uncertain_add_retries_identical_body_and_disables_changes(self):
        await self.session.start(interaction())
        self.plex.request.side_effect = [TimeoutError(), {'id': 'same-task'}]
        await self.button('Confirmer l’ajout').callback(interaction())
        self.assertEqual(self.session.stage, 'uncertain')
        self.assertEqual([c.label for c in self.session.view.children], ['Vérifier l’ajout'])
        await self.button('Vérifier l’ajout').callback(interaction())
        self.assertEqual(self.adds()[0].kwargs['json'], self.adds()[1].kwargs['json'])
        self.assertEqual(self.session.stage, 'done')

    async def test_double_click_during_add_only_sends_one_request(self):
        await self.session.start(interaction())
        started, release = asyncio.Event(), asyncio.Event()
        async def add(*args, **kwargs):
            started.set()
            await release.wait()
            return {'id': 'one-task'}
        self.plex.request.side_effect = add
        confirm = self.button('Confirmer l’ajout')
        pending = asyncio.create_task(confirm.callback(interaction()))
        await started.wait()
        await confirm.callback(interaction())
        release.set()
        await pending
        self.assertEqual(len(self.adds()), 1)

    async def test_quality_change_and_cancel_never_add(self):
        self.result = {'items': [], 'quality_options': ['720p'], 'errors': []}
        await self.session.start(interaction())
        self.assertEqual(self.session.stage, 'quality')
        select = next(c for c in self.session.view.children if hasattr(c, 'options'))
        self.result = {'items': self.items, 'errors': []}
        select._values = ['720p']
        await select.callback(interaction())
        body = self.plex.request.await_args.kwargs['json']
        self.assertEqual(body['quality'], '720p')
        self.assertFalse(body['prefer_quality'])
        self.assertEqual(self.adds(), [])
        await self.button('Annuler').callback(interaction())
        self.assertEqual(self.session.stage, 'cancelled')
        self.assertEqual(self.adds(), [])

    async def test_request_can_be_refined_with_a_private_modal(self):
        self.intent = {**self.intent, 'title': 'À préciser', 'clarification': 'Quel titre ?'}
        await self.session.start(interaction())
        fake = interaction()
        await self.button('Préciser la demande').callback(fake)
        modal = fake.response.send_modal.await_args.args[0]
        self.intent = {**self.intent, 'title': 'Show', 'clarification': None}
        modal.request._value = 'Show saison 1'
        await modal.on_submit(interaction())
        self.assertEqual(self.session.request, 'Show saison 1')
        self.assertEqual(self.session.stage, 'torrent')
        self.assertEqual(self.adds(), [])

    async def test_private_task_events_never_fall_back_to_public_alerts(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'ALERT_CHANNEL_ID': '123', 'DISCORD_NOTIFICATION_CHANNEL_IDS': '123'}):
            store = Store(Path(folder) / 'db')
            try:
                task = store.create_task('private', {'user_id': 42, 'private': True, 'channel_id': None})
                Downloads(store, None).emit(task, 'torrent.completed', {'name': 'Private title'})
                bot = SimpleNamespace(is_ready=lambda: True, get_channel=AsyncMock(), fetch_channel=AsyncMock())
                await Notifications(store, bot).step()
                bot.get_channel.assert_not_called()
                bot.fetch_channel.assert_not_awaited()
                self.assertEqual(store.pending_events(), [])
            finally:
                store.close()

    async def test_status_button_reads_only_confirmed_owners_task_in_private(self):
        await self.session.start(interaction())
        await self.button('Confirmer l’ajout').callback(interaction())
        self.plex.request.side_effect = None
        self.plex.request.return_value = {'id': 'private-task', 'state': 'importing', 'payload': {'user_id': 42}}
        fake = interaction()
        await self.button('Actualiser le suivi').callback(fake)
        self.assertEqual(self.plex.request.await_args.args, ('GET', '/tasks/private-task'))
        self.assertIn('import en cours', fake.edit_original_response.await_args.kwargs['embed'].description)
        self.plex.request.return_value = {'id': 'private-task', 'state': 'completed', 'payload': {'user_id': 99}, 'title': 'OTHER-USERS-TITLE'}
        fake = interaction()
        await self.button('Actualiser le suivi').callback(fake)
        self.assertNotIn('OTHER-USERS-TITLE', str(fake.edit_original_response.await_args))

    async def test_timeout_removes_controls_and_invalidates_confirmation(self):
        await self.session.start(interaction())
        confirm = self.button('Confirmer l’ajout')
        await self.session.view.on_timeout()
        self.session.message.edit.assert_awaited_once()
        await confirm.callback(interaction())
        self.assertEqual(self.adds(), [])

    def series_without_season(self):
        self.session.request = 'Show'
        self.catalog = {'selected': {'title': 'Show', 'kind': 'series', 'year': 2010, 'tmdb_id': 1,
            'aliases': ['Show'], 'seasons': [{'season': 1, 'name': 'Saison 1', 'episodes': 10}, {'season': 2, 'name': 'Saison 2', 'episodes': 42}]}}

    async def test_unspecified_series_requires_season_and_whole_or_episode_before_search(self):
        self.series_without_season()
        await self.session.start(interaction())
        self.assertEqual(self.session.stage, 'season')
        self.plex.request.assert_not_awaited()
        self.assertEqual([o.value for o in self.session.view.select.options], ['1', '2'])
        self.session.view.select._values = ['2']
        await self.session.view.select.callback(interaction())
        self.assertEqual(self.session.stage, 'scope')
        self.plex.request.assert_not_awaited()
        await self.button('Saison entière').callback(interaction())
        body = self.plex.request.await_args.kwargs['json']
        self.assertEqual((body['season'], body['episode']), (2, 0))
        self.assertEqual(self.adds(), [])
        await self.button('Confirmer l’ajout').callback(interaction())
        self.assertEqual(self.adds()[0].kwargs['json']['prefs']['series_mode'], 'complete')

    async def test_episode_menu_uses_tmdb_count_and_twenty_per_page(self):
        self.series_without_season()
        await self.session.start(interaction())
        self.session.view.select._values = ['2']
        await self.session.view.select.callback(interaction())
        await self.button('Un épisode').callback(interaction())
        self.plex.request.assert_not_awaited()
        self.assertEqual(self.session.stage, 'episode')
        self.assertEqual(len(self.session.view.select.options), 20)
        self.assertEqual(self.session.view.select.options[0].label, 'S02E01')
        await self.button('Suivant').callback(interaction())
        self.assertEqual(self.session.view.select.options[0].label, 'S02E21')
        self.session.view.select._values = ['21']
        await self.session.view.select.callback(interaction())
        body = self.plex.request.await_args.kwargs['json']
        self.assertEqual((body['season'], body['episode']), (2, 21))
        self.assertEqual(self.adds(), [])
        await self.button('Confirmer l’ajout').callback(interaction())
        self.assertEqual(self.adds()[0].kwargs['json']['prefs']['series_mode'], 'single')

    async def test_explicit_season_remains_a_whole_season_without_extra_question(self):
        self.series_without_season()
        self.session.request = 'Show saison 2'
        await self.session.start(interaction())
        self.assertEqual(self.session.stage, 'torrent')
        self.assertEqual(self.plex.request.await_args.kwargs['json']['season'], 2)
        self.assertEqual(self.plex.request.await_args.kwargs['json']['episode'], 0)
        self.assertEqual(self.adds(), [])

    async def test_unavailable_tmdb_seasons_asks_for_manual_precision_without_search(self):
        self.session.request = 'Show'
        await self.session.start(interaction())
        self.assertEqual(self.session.stage, 'clarify')
        self.assertIn('Quelle saison', self.session.notice)
        self.plex.request.assert_not_awaited()
        fake = interaction()
        await self.button('Préciser la demande').callback(fake)
        modal = fake.response.send_modal.await_args.args[0]
        modal.request._value = 'Show S02E03'
        await modal.on_submit(interaction())
        self.assertEqual(self.session.stage, 'torrent')
        body = self.plex.request.await_args.kwargs['json']
        self.assertEqual((body['season'], body['episode']), (2, 3))

    async def test_season_choices_reject_other_users_and_out_of_page_values(self):
        self.series_without_season()
        await self.session.start(interaction())
        self.session.view.select._values = ['2']
        await self.session.view.select.callback(interaction(99))
        self.plex.request.assert_not_awaited()
        self.assertEqual(self.session.stage, 'season')
        self.session.view.select._values = ['99']
        await self.session.view.select.callback(interaction())
        self.plex.request.assert_not_awaited()
        self.assertEqual(self.session.stage, 'season')
