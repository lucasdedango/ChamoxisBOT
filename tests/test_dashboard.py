import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import httpx
from dashboard.server import create_app
from dashboard.settings import Settings


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'manager').mkdir()
        (self.root / 'modules/plex').mkdir(parents=True)
        (self.root / 'manager/.env').write_text('MANAGER_ADMIN_API_KEY=admin-secret\nMANAGER_API_KEY=module-secret\nPLEX_MODULE_API_KEY=plex-secret\nDISCORD_ADMIN_IDS=369545955252502528\nSEARCH_DEFAULT_QUALITY=1080p\n')
        (self.root / 'modules/plex/.env').write_text('QBIT_PASS=qbit-secret\nDISABLE_TORRENT_DOWNLOAD=true\n')
        self.intent = {'title': 'Show', 'kind': 'series', 'year': None, 'quality': '1080p', 'language': None, 'season': 11, 'episode': 0, 'clarification': None}
        self.task = {'id': 'task-id', 'payload': {'title': 'Show.S11.MULTI.1080p', 'user_id': 369545955252502528, 'link': 'https://indexer/?apikey=PRIVATE', 'prefs': {}},
                     'state': 'downloading', 'updated': 1, 'result': {'hash': 'abcdef', 'progress': .5}}
        async def backend(method, path, **kwargs):
            if path == '/ai/analyze': return {'intent': self.intent}
            if path == '/search': return {'items': [{'title': 'Show.S11.MULTI.1080p', 'size': '1000', 'seeders': '4', 'link': 'result:opaque'}], 'errors': []}
            if path == '/downloads': return self.task
            if path == '/tasks': return [self.task]
            if path.startswith('/tasks/'): return self.task
            if path.startswith('/torrents/'): return {'state': 'downloading', 'progress': .5, 'dlspeed': 2, 'magnet_uri': 'PRIVATE', 'tracker': 'PRIVATE'}
            if path == '/health': return {'status': 'ok', 'discord': 'ready'}
            if path == '/services': return []
            if path == '/storage': return {'roots': [], 'suggestions': []}
            raise AssertionError(path)
        self.backend = AsyncMock(side_effect=backend)
        self.updater = SimpleNamespace(prepare=lambda b: {'branch': b}, apply=Mock(return_value={'updated': True}))
        app = create_app(self.root, lambda *a, **k: SimpleNamespace(request=self.backend, url='http://127.0.0.1:8760'), self.updater, launch_token='one-time')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8765')
        self.origin = {'Origin': 'http://127.0.0.1:8765'}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def login(self, key='admin-secret'):
        response = await self.client.post('/api/login', json={'credential': key}, headers=self.origin)
        self.assertEqual(response.status_code, 200)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.headers = {**self.origin, 'X-CSRF-Token': response.json()['csrf']}

    async def test_auth_origin_csrf_and_host_are_required(self):
        self.assertEqual((await self.client.get('/api/config')).status_code, 401)
        self.assertEqual((await self.client.post('/api/login', json={'credential': 'admin-secret'})).status_code, 403)
        self.assertEqual((await self.client.get('/api/config', headers={'Host': 'evil.test'})).status_code, 403)
        await self.login()
        self.assertEqual((await self.client.post('/api/config', json={'group': 'manager', 'changes': {'SEARCH_DEFAULT_QUALITY': '720p'}}, headers=self.origin)).status_code, 403)
        response = await self.client.get('/api/config')
        self.assertNotIn('admin-secret', response.text)
        self.assertNotIn('qbit-secret', response.text)

    async def test_launch_token_is_single_use_and_logout_revokes_session(self):
        await self.login('one-time')
        self.assertEqual((await self.client.post('/api/login', json={'credential': 'one-time'}, headers=self.origin)).status_code, 401)
        self.assertEqual((await self.client.post('/api/logout', json={}, headers=self.headers)).status_code, 200)
        self.assertEqual((await self.client.get('/api/config')).status_code, 401)

    async def test_config_write_backs_up_and_preserves_secret(self):
        await self.login()
        response = await self.client.post('/api/config', json={'group': 'manager', 'changes': {'SEARCH_DEFAULT_QUALITY': '720p'}}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.root / response.json()['backup']).exists())
        values = Settings(self.root).values('manager')
        self.assertEqual(values['MANAGER_ADMIN_API_KEY'], 'admin-secret')
        self.assertEqual(values['SEARCH_DEFAULT_QUALITY'], '720p')
        response = await self.client.post('/api/config', json={'group': '../outside', 'changes': {'FILE': 'value'}}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = await self.client.post('/api/config', json={'group': 'manager', 'changes': {'SEARCH_DEFAULT_QUALITY': '8K'}}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    async def test_search_requires_server_proposal_and_keeps_stable_id(self):
        await self.login()
        result = (await self.client.post('/api/search', json={'text': 'Show S11 en 1080p'}, headers=self.headers)).json()
        self.assertNotIn('result:opaque', json.dumps(result))
        self.assertEqual(result['user_id'], '369545955252502528')
        self.assertEqual([c for c in self.backend.await_args_list if c.args[1] == '/downloads'], [])
        body = {'proposal': result['proposal'], 'index': 0, 'confirmed': True}
        for _ in range(2):
            self.assertEqual((await self.client.post('/api/search/confirm', json=body, headers=self.headers)).status_code, 200)
        calls = [c for c in self.backend.await_args_list if c.args[1] == '/downloads']
        self.assertEqual(calls[0].kwargs['json'], calls[1].kwargs['json'])
        self.assertEqual(calls[0].kwargs['json']['user_id'], 369545955252502528)
        body['proposal'] = 'forged'
        self.assertEqual((await self.client.post('/api/search/confirm', json=body, headers=self.headers)).status_code, 409)

    async def test_overview_and_task_detail_hide_links_and_tracker_credentials(self):
        await self.login()
        response = await self.client.get('/api/overview')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE', response.text)
        self.assertEqual(response.json()['tasks'][0]['user_id'], '369545955252502528')
        response = await self.client.get('/api/tasks/task-id')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE', response.text)

    async def test_log_tail_masks_module_and_manager_secrets(self):
        await self.login()
        path = self.root / 'data/plex/plex.log'
        path.parent.mkdir(parents=True)
        path.write_text('WARNING qbit-secret admin-secret token=abcdefghi\nINFO safe\n')
        response = await self.client.get('/api/logs?source=plex&level=WARNING')
        self.assertEqual(response.status_code, 200)
        for secret in ('qbit-secret', 'admin-secret', 'abcdefghi'):
            self.assertNotIn(secret, response.text)
        self.assertEqual((await self.client.get('/api/logs?source=../../etc/passwd')).status_code, 400)

    async def test_update_apply_refuses_running_services(self):
        await self.login()
        listening = [SimpleNamespace(laddr=SimpleNamespace(port=8760), status='LISTEN')]
        with patch('dashboard.server.psutil.net_connections', return_value=listening):
            response = await self.client.post('/api/updates/apply', json={'plan': 'plan', 'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.updater.apply.assert_not_called()

    async def test_update_refuses_running_module_even_without_a_listening_port(self):
        await self.login()
        process = SimpleNamespace(info={'cmdline': ['python.exe', '-m', 'modules.plex.main']})
        with patch('dashboard.server.psutil.net_connections', return_value=[]), patch('dashboard.server.psutil.process_iter', return_value=[process]):
            response = await self.client.post('/api/updates/apply', json={'plan': 'plan', 'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.updater.apply.assert_not_called()

    async def test_confirmed_update_can_apply_after_service_shutdown(self):
        await self.login()
        with patch('dashboard.server.psutil.net_connections', return_value=[]), patch('dashboard.server.psutil.process_iter', return_value=[]):
            response = await self.client.post('/api/updates/apply', json={'plan': 'plan', 'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.updater.apply.assert_called_once_with('plan')
