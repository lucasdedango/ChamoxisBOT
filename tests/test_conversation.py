import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from chamoxis_common.store import Store
from manager.discord.conversation import Conversation


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite3")
        self.env = patch.dict(os.environ, {"DISCORD_CONVERSATION_CHANNEL_IDS": "123", "DISCORD_ALLOWED_USER_IDS": "42,43", "DISCORD_ADMIN_IDS": ""})
        self.env.start()
        self.now = 0
        self.sent_id = 1000
        async def send(*args, **kwargs):
            self.sent_id += 1
            return SimpleNamespace(id=self.sent_id)
        self.channel = SimpleNamespace(id=123, send=AsyncMock(side_effect=send))
        self.intent = {"title": "Charlie et la Chocolaterie", "kind": "movies", "year": 2005, "quality": "1080p",
                       "language": "français", "season": 0, "episode": 0, "clarification": None}
        self.items = [{"title": "Charlie.2005.MULTI.1080p", "enclosure": "result:first"},
                      {"title": "Charlie.2005.FRENCH.720p", "enclosure": "result:second"}]
        self.manager = SimpleNamespace(request=AsyncMock(return_value={"intent": self.intent}))
        async def request(method, path, **kwargs):
            if path == "/search":
                return {"items": self.items, "errors": []}
            if path == "/downloads":
                return {"id": "task-id"}
            raise AssertionError(path)
        self.plex = SimpleNamespace(request=AsyncMock(side_effect=request))
        self.worker = Conversation(self.store, lambda: self.manager, lambda: self.plex, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def message(self, content, user=42, message_id=2000, channel=None, bot=False, reference=None):
        return SimpleNamespace(content=content, author=SimpleNamespace(id=user, bot=bot),
                               guild=SimpleNamespace(id=1), channel=channel or self.channel,
                               id=message_id, reference=reference)

    def downloads(self):
        return [call for call in self.plex.request.await_args_list if call.args[1] == "/downloads"]

    async def test_search_is_broad_and_add_requires_following_yes(self):
        await self.worker.handle(self.message("bot cherche Charlie et la Chocolaterie de 2005 en français en 1080p", message_id=100))
        self.assertEqual(self.downloads(), [])
        body = self.plex.request.await_args.kwargs["json"]
        self.assertEqual(body["query"], "Charlie et la Chocolaterie")
        self.assertEqual(body["year"], 2005)
        self.assertTrue(body["rank_preferences"])
        await self.worker.handle(self.message("oui"))
        body = self.downloads()[0].kwargs["json"]
        self.assertTrue(body["confirmed"])
        self.assertEqual(body["user_id"], 42)
        self.assertEqual(body["link"], "result:first")
        await self.worker.handle(self.message("oui", message_id=2001))
        self.assertEqual(len(self.downloads()), 1)

    async def test_other_users_and_unconfigured_channels_cannot_confirm(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        await self.worker.handle(self.message("oui", user=43))
        await self.worker.handle(self.message("oui", user=99))
        await self.worker.handle(self.message("oui", bot=True))
        await self.worker.handle(self.message("bot cherche Charlie", channel=SimpleNamespace(id=999)))
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.manager.request.await_count, 1)

    async def test_change_selection_requires_new_confirmation(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        await self.worker.handle(self.message("2"))
        self.assertEqual(self.downloads(), [])
        await self.worker.handle(self.message("oui", message_id=2001))
        self.assertEqual(self.downloads()[0].kwargs["json"]["link"], "result:second")

    async def test_expiry_and_cancellation_do_not_add(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        self.now = 301
        await self.worker.handle(self.message("oui"))
        self.assertEqual(self.downloads(), [])
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=2001))
        await self.worker.handle(self.message("non", message_id=2002))
        await self.worker.handle(self.message("oui", message_id=2003))
        self.assertEqual(self.downloads(), [])

    async def test_yes_before_proposal_and_reply_to_other_message_are_ignored(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        await self.worker.handle(self.message("oui", message_id=101))
        await self.worker.handle(self.message("oui", reference=SimpleNamespace(message_id=777)))
        self.assertEqual(self.downloads(), [])

    async def test_failed_add_retry_reuses_exact_same_request(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        self.plex.request.side_effect = [TimeoutError(), {"id": "task-id"}]
        await self.worker.handle(self.message("oui"))
        await self.worker.handle(self.message("oui", message_id=2001))
        calls = self.downloads()
        self.assertEqual(calls[0].kwargs["json"], calls[1].kwargs["json"])
        self.assertEqual(self.worker.locks, {})

    async def test_clarification_response_and_restart_resume(self):
        self.manager.request.side_effect = [{"intent": {**self.intent, "clarification": "Quelle année ?"}}, {"intent": self.intent}]
        await self.worker.handle(self.message("bot cherche Charlie", message_id=100))
        self.assertEqual(self.plex.request.await_count, 0)
        await self.worker.handle(self.message("2005"))
        self.assertIn("2005", self.manager.request.await_args.kwargs["json"]["text"])
        restored = Conversation(self.store, lambda: self.manager, lambda: self.plex, clock=lambda: self.now)
        await restored.handle(self.message("oui", message_id=2001))
        self.assertEqual(len(self.downloads()), 1)

    async def test_concurrent_yes_messages_add_once(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        await asyncio.gather(self.worker.handle(self.message("oui")), self.worker.handle(self.message("oui", message_id=2001)))
        self.assertEqual(len(self.downloads()), 1)
        self.assertEqual(self.worker.locks, {})
