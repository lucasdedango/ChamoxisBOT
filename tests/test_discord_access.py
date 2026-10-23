import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from chamoxis_common.store import Store
from manager.core import permissions
from manager.discord import bot as ui
from manager.discord.commands.admin import CancelTracking
from manager.discord.commands.plex import ConfirmDownload, DownloadStatusView, operations
from manager.discord.conversation import Conversation


def interaction(user=42, command='recherchetorrent'):
    return SimpleNamespace(id=123, user=SimpleNamespace(id=user), channel_id=456,
        command=SimpleNamespace(qualified_name=command),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        permissions.configure(self.store)
        self.env = patch.dict(os.environ, {'DISCORD_ADMIN_IDS': '1', 'DISCORD_ALLOWED_USER_IDS': '42'})
        self.env.start()

    async def asyncTearDown(self):
        permissions.configure(None)
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    async def test_added_and_revoked_access_persist_across_restart(self):
        self.assertFalse(permissions.allowed_user(43))
        permissions.update_access(1, 43, allow=True)
        self.assertTrue(permissions.allowed_user(43))
        permissions.update_access(1, 42, allow=False)
        self.assertFalse(permissions.allowed_user(42))  # Explicit revoke overrides .env.
        self.store.close()
        self.store = Store(self.path)
        permissions.configure(self.store)
        self.assertTrue(permissions.allowed_user(43))
        self.assertFalse(permissions.allowed_user(42))
        self.assertTrue(permissions.allowed_user(1))
        self.assertFalse(permissions.administrator(43))

    async def test_no_unexpected_lockout_when_adding_in_open_mode(self):
        with patch.dict(os.environ, {'DISCORD_ALLOWED_USER_IDS': ''}):
            permissions.update_access(1, 43, allow=True)
            self.assertTrue(permissions.allowed_user(99))
            permissions.update_access(1, mode='restricted')
            self.assertFalse(permissions.allowed_user(99))
            self.assertTrue(permissions.allowed_user(43))
            permissions.update_access(1, mode='open')
            permissions.update_access(1, 99, allow=False)
            self.assertFalse(permissions.allowed_user(99))
            self.assertTrue(permissions.allowed_user(100))

    async def test_non_admin_cannot_change_access_or_revoke_an_admin(self):
        with self.assertRaises(PermissionError):
            permissions.update_access(42, 43, allow=True)
        with self.assertRaises(ValueError):
            permissions.update_access(1, 1, allow=False)
        self.assertEqual(permissions.access_settings(), {})

    async def test_help_is_accessible_but_other_commands_require_access_without_registration(self):
        self.assertTrue(await ui.check_command_access(interaction(99, 'aide')))
        self.assertFalse(await ui.check_command_access(interaction(99)))
        self.assertTrue(await ui.check_command_access(interaction(42)))
        self.assertTrue(await ui.check_command_access(interaction(1, 'admin utilisateurs ajouter')))

    async def test_every_admin_handler_rejects_normal_users_before_backend_access(self):
        admin = ui.bot.tree.get_command('admin')
        with patch('manager.discord.commands.admin.plex_client') as plex, patch('manager.discord.commands.server.manager_client') as manager:
            for name, args in [('torrents', ()), ('annuler_suivis', ()), ('services', ()), ('relancer', ('plex',))]:
                fake = interaction()
                await admin.get_command(name).callback(fake, *args)
                fake.response.send_message.assert_awaited_once()
            for name, args in [('ajouter', (SimpleNamespace(id=43, bot=False),)), ('retirer', (SimpleNamespace(id=43),)), ('liste', ()), ('mode', ('open',))]:
                fake = interaction()
                await admin.get_command('utilisateurs').get_command(name).callback(fake, *args)
                fake.response.send_message.assert_awaited_once()
            plex.assert_not_called()
            manager.assert_not_called()
        self.assertEqual(permissions.access_settings(), {})

    async def test_admin_slash_commands_change_permissions_immediately(self):
        users = ui.bot.tree.get_command('admin').get_command('utilisateurs')
        member = SimpleNamespace(id=43, bot=False)
        await users.get_command('ajouter').callback(interaction(1), member)
        self.assertTrue(permissions.allowed_user(43))
        await users.get_command('retirer').callback(interaction(1), member)
        self.assertFalse(permissions.allowed_user(43))

    async def test_text_commands_follow_admin_grants_and_revocations_in_allowed_channels(self):
        conversation = Conversation(self.store)
        channel = SimpleNamespace(id=456, send=AsyncMock())
        message = SimpleNamespace(id=10, author=SimpleNamespace(id=43, bot=False), guild=SimpleNamespace(id=1),
                                  channel=channel, content='man cherche Show S01')
        with patch.dict(os.environ, {'DISCORD_CONVERSATION_CHANNEL_IDS': '456'}):
            await conversation.handle(message)
            channel.send.assert_not_awaited()
            permissions.update_access(1, 43, allow=True)
            await conversation.handle(message)
            self.assertEqual(channel.send.await_count, 1)
            invitation = channel.send.await_args.kwargs['view']
            permissions.update_access(1, 43, allow=False)
            await conversation.handle(message)
            self.assertEqual(channel.send.await_count, 1)
            blocked = interaction(43)
            await invitation.open.callback(blocked)
            blocked.response.send_message.assert_awaited_once()
            blocked.response.defer.assert_not_awaited()

    async def test_revocation_during_discord_ack_prevents_download_dispatch(self):
        confirm = ConfirmDownload(42, 'result:abc', 'Film', {'kind': 'movies'}, 'stable')
        fake = interaction()
        fake.response.defer.side_effect = lambda **_: permissions.update_access(1, 42, allow=False)
        with patch('manager.discord.commands.plex.plex_client') as backend:
            await confirm.confirm.callback(fake)
            backend.assert_not_called()

    async def test_revocation_blocks_existing_private_confirmations_and_status_buttons(self):
        confirm = ConfirmDownload(42, 'result:abc', 'Film', {'kind': 'movies'}, 'stable')
        status = DownloadStatusView(42)
        permissions.update_access(1, 42, allow=False)
        with patch('manager.discord.commands.plex.plex_client') as backend:
            await confirm.confirm.callback(interaction())
            await status.refresh.callback(interaction())
            backend.assert_not_called()

    async def test_personal_downloads_do_not_expose_other_users_even_to_admin(self):
        tasks = [{'id': 'mine', 'payload': {'user_id': 1, 'title': 'Own film'}, 'state': 'downloading'},
                 {'id': 'other', 'payload': {'user_id': 42, 'title': 'Secret film'}, 'state': 'downloading'}]
        backend = SimpleNamespace(request=AsyncMock(side_effect=[tasks, {'downloads': [
            {'id': 'mine', 'progress': .5, 'observation': 'Transfert en cours.'}, {'id': 'other', 'progress': .8}]}]))
        fake = interaction(1)
        with patch('manager.discord.commands.plex.plex_client', return_value=backend):
            await operations(fake)
        text = fake.followup.send.await_args.args[0]
        self.assertIn('Own film', text)
        self.assertIn('50%', text)
        self.assertNotIn('Secret', text)
        self.assertNotIn('other', text)
        self.assertTrue(fake.followup.send.await_args.kwargs['ephemeral'])

    async def test_cancelling_tracking_requires_owner_admin_confirmation_and_uses_snapshot_only(self):
        view = CancelTracking(1, ['existing'])
        backend = SimpleNamespace(request=AsyncMock(side_effect=[{'state': 'downloading'}, {}]))
        with patch('manager.discord.commands.admin.plex_client', return_value=backend):
            await view.confirm.callback(interaction(42))
            backend.request.assert_not_awaited()
            await view.confirm.callback(interaction(1))
            self.assertEqual([call.args[1] for call in backend.request.await_args_list], ['/tasks/existing', '/tasks/existing/cancel'])
            await view.confirm.callback(interaction(1))
            self.assertEqual(backend.request.await_count, 2)

    async def test_cancel_tracking_expiry_and_admin_revocation_prevent_action(self):
        for mode in ('expired', 'revoked'):
            view = CancelTracking(1, ['existing'])
            if mode == 'expired':
                await view.on_timeout()
            with patch.dict(os.environ, {'DISCORD_ADMIN_IDS': '' if mode == 'revoked' else '1'}), patch('manager.discord.commands.admin.plex_client') as backend:
                await view.confirm.callback(interaction(1))
                backend.assert_not_called()
