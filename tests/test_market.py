import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import discord
from aiohttp.test_utils import TestClient, TestServer
from filelock import FileLock
from modules.market import runtime as rt
from modules.market.access import Access
from modules.market.commands import DeleteSearch, register
from modules.market.config import DEFAULTS, settings
from modules.market.main import initialize, import_legacy, health_app


def interaction(user=42, channel=123):
    return SimpleNamespace(user=SimpleNamespace(id=user), channel_id=channel,
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


class MarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / 'market'
        self.config = {'data': self.data, 'token': 'dummy-market-token', 'channel': 123, 'guild': 0,
                       'admins': {1}, 'users': {42, 43}, 'port': 8762, 'config': dict(DEFAULTS)}
        self.store = initialize(self.config)
        self.discord_client = discord.Client(intents=discord.Intents.default())
        self.tree = discord.app_commands.CommandTree(self.discord_client)
        register(self.tree)

    async def asyncTearDown(self):
        rt.STOP_EVENT.set()
        rt.MAIN_LOOP = None
        await self.discord_client.close()
        self.store.close()
        self.tmp.cleanup()

    def command(self, name):
        return self.tree.get_command('recherche').get_command(name)

    async def add(self, user=42, url='https://www.vinted.fr/catalog?search_text=telephone'):
        await self.command('ajouter').callback(interaction(user), url, 'Téléphone', 10.0, 20.0)
        return rt.SEARCHES[-1]

    def test_only_market_commands_are_registered(self):
        self.assertEqual({command.name for command in self.tree.get_commands()}, {'recherche', 'admin', 'etat', 'aide'})
        for command in self.tree.get_commands():
            command.to_dict(self.tree)

    async def test_user_can_add_owned_search_and_access_persists_independently(self):
        search = await self.add()
        self.assertEqual(search['owner_id'], 42)
        self.assertEqual(json.loads(rt.SEARCHES_PATH.read_text())[0]['id'], search['id'])
        self.assertFalse(rt.ACCESS.allowed(99))
        admin = self.tree.get_command('admin').get_command('utilisateurs')
        await admin.get_command('ajouter').callback(interaction(1), SimpleNamespace(id=99, bot=False))
        self.assertTrue(Access(self.store, {1}).allowed(99))
        self.assertFalse(rt.ACCESS.administrator(99))
        with patch.dict(os.environ, {'DISCORD_ADMIN_IDS': '100', 'DISCORD_ALLOWED_USER_IDS': '100'}):
            self.assertFalse(rt.ACCESS.allowed(100))

    async def test_non_admin_and_wrong_channel_cannot_grant_access_or_add_search(self):
        admin = self.tree.get_command('admin').get_command('utilisateurs')
        await admin.get_command('ajouter').callback(interaction(42), SimpleNamespace(id=99, bot=False))
        await self.command('ajouter').callback(interaction(99), 'https://vinted.fr/catalog?x=1', 'Denied')
        await self.command('ajouter').callback(interaction(42, 999), 'https://vinted.fr/catalog?x=1', 'Wrong channel')
        self.assertFalse(rt.ACCESS.allowed(99))
        self.assertEqual(rt.SEARCHES, [])

    async def test_user_cannot_read_modify_delete_or_check_someone_elses_search(self):
        search = await self.add(user=43)
        with patch.object(rt, 'fetch_search') as fetch:
            for name, args in [('details', ()), ('pause', ()), ('reprendre', ()), ('supprimer', ()), ('verifier', ()), ('intervalle', (5.0, 10.0))]:
                fake = interaction()
                await self.command(name).callback(fake, search['id'], *args)
                fake.response.send_message.assert_awaited_once()
                self.assertIn('non autorisée', fake.response.send_message.await_args.args[0])
            fetch.assert_not_called()
        self.assertEqual(len(rt.SEARCHES), 1)

    async def test_quota_duplicates_and_intervals_are_validated(self):
        rt.CONFIG['max_searches_per_user'] = 1
        await self.add()
        await self.command('ajouter').callback(interaction(), 'https://vinted.fr/catalog?search_text=other', 'Other')
        self.assertEqual(len(rt.SEARCHES), 1)
        with self.assertRaises(ValueError):
            rt.validate_interval(float('nan'), 10)

    async def test_revocation_suspends_fetches_and_late_browser_callbacks(self):
        search = await self.add()
        rt.ACCESS.set_user(1, 42, False)
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(rt, 'fetch_search') as fetch, patch.object(rt, 'notification_channel', AsyncMock(return_value=channel)):
            ok, _ = await rt.run_search(search)
            await rt._process_lbc_persistent_items(search, [rt.Listing('leboncoin', '1', 'Phone', '20', 'EUR', 'https://leboncoin.fr/ad/1')])
            self.assertFalse(ok)
            fetch.assert_not_called()
            channel.send.assert_not_awaited()
        rt.ACCESS.set_user(1, 42, True)
        self.assertTrue(rt.search_is_active(search))

    async def test_first_pass_is_silent_new_items_are_sent_and_seen_state_survives_restart(self):
        search = await self.add()
        first = rt.Listing('vinted', '1', 'Phone', '20', 'EUR', 'https://vinted.fr/items/1-phone')
        second = rt.Listing('vinted', '2', 'Phone 2', '25', 'EUR', 'https://vinted.fr/items/2-phone')
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(rt, 'notification_channel', AsyncMock(return_value=channel)), patch.object(rt, 'fetch_search', side_effect=[[first], [first, second]]):
            self.assertTrue((await rt.run_search(search))[0])
            channel.send.assert_not_awaited()
            self.assertTrue((await rt.run_search(search))[0])
            channel.send.assert_awaited_once()
            self.assertEqual(channel.send.await_args.kwargs['allowed_mentions'].to_dict()['parse'], [])
        self.store.close()
        self.store = initialize(self.config)
        self.assertEqual(set(rt.STATE['searches'][search['id']]['seen_ids']), {'1', '2'})

    async def test_block_backoff_cannot_be_reset_by_manual_check_or_interval_edit(self):
        search = await self.add()
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(rt, 'notification_channel', AsyncMock(return_value=channel)), patch.object(rt, 'fetch_search', side_effect=rt.FetchBlocked('HTTP 403', 403)):
            self.assertFalse((await rt.run_search(search))[0])
        due = rt.next_due[search['id']]
        self.assertGreater(due, time.monotonic()+600)
        with patch.object(rt, 'fetch_search') as fetch:
            await self.command('verifier').callback(interaction(), search['id'])
            fetch.assert_not_called()
        await self.command('intervalle').callback(interaction(), search['id'], 5.0, 5.0)
        self.assertGreaterEqual(rt.next_due[search['id']], due)

    async def test_delete_confirmation_checks_owner_and_revoked_access(self):
        search = await self.add()
        view = DeleteSearch(42, search['id'])
        await view.confirm.callback(interaction(43))
        self.assertEqual(len(rt.SEARCHES), 1)
        rt.ACCESS.set_user(1, 42, False)
        await view.confirm.callback(interaction())
        self.assertEqual(len(rt.SEARCHES), 1)
        rt.ACCESS.set_user(1, 42, True)
        await view.confirm.callback(interaction())
        self.assertEqual(rt.SEARCHES, [])
        self.assertEqual(json.loads(rt.SEARCHES_PATH.read_text()), [])
        self.assertFalse(rt.search_is_active(search))

    async def test_only_admin_can_request_manual_browser_verification(self):
        search = await self.add(url='https://www.leboncoin.fr/recherche?text=telephone')
        command = self.tree.get_command('admin').get_command('leboncoin_verifier')
        with patch.object(rt, '_start_lbc_manual_verification', return_value=True) as browser:
            await command.callback(interaction(42), search['id'])
            browser.assert_not_called()
            await command.callback(interaction(1), search['id'])
            browser.assert_called_once()

    def test_search_urls_and_cross_domain_redirects_cannot_target_arbitrary_hosts(self):
        for url in ('http://www.vinted.fr/catalog', 'https://vinted.fr.evil.test/catalog', 'https://internal.vinted.fr/catalog',
                    'https://www.vinted.fr:8443/catalog', 'https://user:password@www.vinted.fr/catalog',
                    'https://127.0.0.1/catalog', 'https://www.leboncoin.fr/ad/1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                rt.validate_search_url(url)
        transport = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=302, headers={'Location': 'http://127.0.0.1/private'}, close=Mock())))
        with self.assertRaises(ValueError):
            rt._safe_get(transport, 'https://www.vinted.fr/catalog', headers={}, timeout=1)
        transport.get.assert_called_once()
        self.assertFalse(transport.get.call_args.kwargs['allow_redirects'])

    def test_browser_tabs_do_not_mix_different_users_search_urls(self):
        old = SimpleNamespace(url='https://www.leboncoin.fr/recherche?text=other')
        new = SimpleNamespace(goto=Mock())
        context = SimpleNamespace(pages=[old], new_page=Mock(return_value=new))
        wanted = 'https://www.leboncoin.fr/recherche?text=phone'
        self.assertIs(rt._find_lbc_page(context, wanted), new)
        new.goto.assert_called_once()
        matching = SimpleNamespace(url=wanted)
        context.pages.append(matching)
        self.assertIs(rt._find_lbc_page(context, wanted), matching)

    def test_vinted_and_leboncoin_parsers_keep_listing_data(self):
        vinted = '<a href="/items/123-phone" title="Phone, Marque: Example, 25,50 €">Phone</a>'
        self.assertEqual(rt.parse_vinted(vinted)[0].item_id, '123')
        payload = {'props': {'pageProps': {'searchData': {'ads': [
            {'list_id': 456, 'subject': 'Phone', 'price': [30], 'url': 'https://www.leboncoin.fr/ad/456', 'images': {}}]}}}}
        leboncoin = '<script id="__NEXT_DATA__" type="application/json">'+json.dumps(payload)+'</script>'
        self.assertEqual(rt.parse_leboncoin(leboncoin)[0].item_id, '456')

    def test_config_uses_separate_token_and_supports_old_env_file_names_only_locally(self):
        path = Path(self.tmp.name) / 'market.env'
        path.write_text('DISCORD_TOKEN=legacy-market-token\nDISCORD_CHANNEL_ID=123\nMARKET_ADMIN_IDS=1\n')
        with patch.dict(os.environ, {'MARKET_ENV_FILE': str(path), 'DISCORD_TOKEN': 'plex-must-not-be-used'}, clear=True):
            self.assertEqual(settings()['token'], 'legacy-market-token')
        with patch.dict(os.environ, {'MARKET_ENV_FILE': str(path)+'absent', 'DISCORD_TOKEN': 'plex-must-not-be-used'}, clear=True):
            self.assertEqual(settings()['token'], '')

    def test_legacy_import_preserves_state_and_never_copies_env_or_overwrites(self):
        source = Path(self.tmp.name) / 'old'
        source.mkdir()
        (source / '.env').write_text('DISCORD_TOKEN=must-stay-local')
        (source / 'searches.json').write_text(json.dumps([{'id': 'legacy-vinted', 'url': 'https://www.vinted.fr/catalog?x=1', 'min_minutes': 12, 'max_minutes': 18}]))
        (source / 'state.json').write_text(json.dumps({'initialized': True, 'seen_ids': ['123']}))
        import_legacy(source, self.data)
        self.assertFalse((self.data / '.env').exists())
        self.store.close()
        self.store = initialize(self.config)
        self.assertEqual(rt.SEARCHES[0]['owner_id'], 1)
        self.assertEqual(rt.STATE['searches']['legacy-vinted']['seen_ids'], ['123'])
        with self.assertRaises(ValueError):
            import_legacy(source, self.data)

    async def test_loopback_health_distinguishes_discord_readiness_without_exposing_data(self):
        bot = SimpleNamespace(is_ready=lambda: False)
        async with TestClient(TestServer(health_app(bot))) as client:
            response = await client.get('/health')
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())['module'], 'market')
            bot.is_ready = lambda: True
            response = await client.get('/health')
            self.assertEqual(response.status, 200)
            self.assertNotIn('dummy-market-token', await response.text())

    def test_entrypoint_check_is_offline_and_second_instance_is_rejected(self):
        env = {**os.environ, 'MARKET_ENV_FILE': str(self.data / 'absent.env'), 'MARKET_DATA_DIR': str(self.data),
               'MARKET_DISCORD_TOKEN': 'dummy-market-token', 'MARKET_CHANNEL_ID': '123', 'MARKET_ADMIN_IDS': '1'}
        command = [sys.executable, '-m', 'modules.market.main', '--check']
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Aucun accès réseau', result.stdout)
        with FileLock(str(self.data / 'market.lock'), timeout=0):
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
