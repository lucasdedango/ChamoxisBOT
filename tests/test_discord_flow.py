import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from manager.discord import bot as ui
from manager.discord.commands.plex import ConfirmDownload, NaturalRequest
from manager.ai.tools import ToolRegistry, Tool
from shared.schemas import Analyze


def interaction(user_id=42):
    return SimpleNamespace(id=123456, user=SimpleNamespace(id=user_id), channel_id=123,
                           response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False),
                           followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())


class DiscordFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_historical_commands_and_groups_registered(self):
        names = {command.name for command in ui.bot.tree.get_commands()}
        self.assertEqual(names, {"status", "rssfeed", "recherchetorrent", "addtorrent", "cleartorrents", "info", "plex", "server", "ai"})
        group = ui.bot.tree.get_command("plex")
        for name in {"status", "rssfeed", "recherchetorrent", "addtorrent", "cleartorrents", "info"}:
            self.assertIs(group.get_command(name).callback, ui.bot.tree.get_command(name).callback)
        self.assertIs(ui.bot.tree.interaction_check, ui.ensure_user_has_read_info)

    async def test_search_menu_is_built_from_backend_response(self):
        client = SimpleNamespace(request=AsyncMock(return_value={"items": [{"title": "Dune.2021.1080p", "enclosure": "result:abc", "source": "C411", "size": "12345"}], "errors": []}))
        fake = interaction()
        with patch.object(ui, "plex_client", return_value=client):
            await ui.send_torznab_results(fake, "Dune", "movies", prefs={"target_name": "Dune (2021)"})
        kwargs = fake.followup.send.await_args.kwargs
        self.assertIsInstance(kwargs["view"], ui.RssView)
        self.assertIn("Dune", kwargs["embed"].description)

    async def test_confirmation_owner_and_stable_request(self):
        fake = interaction()
        view = ConfirmDownload(42, "result:abc", "Dune", {"kind": "movies", "target_name": "Dune (2021)"}, "stable-request")
        self.assertFalse(await view.interaction_check(interaction(99)))
        client = SimpleNamespace(request=AsyncMock(return_value={"id": "task-123"}))
        with patch("manager.discord.commands.plex.plex_client", return_value=client), patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": "42"}):
            await view.confirm.callback(fake)
        self.assertEqual(client.request.await_args.kwargs["json"]["request_id"], "stable-request")
        self.assertTrue(client.request.await_args.kwargs["json"]["confirmed"])
        self.assertTrue(all(item.disabled for item in view.children))

    async def test_unauthorized_confirmation_performs_no_backend_action(self):
        fake = interaction(99)
        view = ConfirmDownload(99, "result:abc", "Dune", {"kind": "movies"}, "request")
        client = SimpleNamespace(request=AsyncMock())
        with patch("manager.discord.commands.plex.plex_client", return_value=client), patch.dict(os.environ, {"DISCORD_ALLOWED_USER_IDS": "42", "DISCORD_ADMIN_IDS": ""}):
            await view.confirm.callback(fake)
        client.request.assert_not_awaited()

    async def test_natural_search_preserves_year_language_and_episode(self):
        fake = interaction()
        intent = {"title": "Andor", "year": 2022, "kind": "series", "quality": "1080p", "language": "français", "season": 2, "episode": 1}
        view = NaturalRequest(42, intent)
        client = SimpleNamespace(request=AsyncMock(return_value={"matches": []}))
        with patch("manager.discord.commands.plex.plex_client", return_value=client), patch.object(ui, "send_torznab_results", AsyncMock()) as search:
            await view.search.callback(fake)
        self.assertEqual(search.await_args.args[1], "Andor 2022 S02E01")
        self.assertEqual(search.await_args.kwargs["language"], "français")
        self.assertEqual(search.await_args.kwargs["prefs"]["episode"], 1)

    async def test_tool_registry_requires_permission_confirmation_and_schema(self):
        registry = ToolRegistry()
        handler = AsyncMock(return_value="ok")
        registry.register("analyze", Tool(Analyze, handler, lambda user: user == 42))
        for user, confirmed in [(99, True), (42, False)]:
            with self.assertRaises(PermissionError):
                await registry.invoke("analyze", {"text": "Dune"}, user, confirmed)
        with self.assertRaises(ValueError):
            await registry.invoke("analyze", {"command": "arbitrary"}, 42, True)
        self.assertEqual(await registry.invoke("analyze", {"text": "Dune"}, 42, True), "ok")
