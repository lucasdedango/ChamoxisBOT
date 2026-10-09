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

    async def test_natural_series_request_uses_search_and_requires_yes(self):
        self.intent.update(title="Grey’s Anatomy", kind="series", year=None, season=11)
        async def request(method, path, **kwargs):
            if path == "/ai/route":
                return {"route": {"action": "search", "request": "Grey’s Anatomy saison 11"}}
            return {"intent": self.intent}
        self.manager.request.side_effect = request
        await self.worker.handle(self.message("bot, tu peux me trouver greys anatomy S11?", message_id=100))
        body = self.plex.request.await_args.kwargs["json"]
        self.assertEqual(body["query"], "Grey’s Anatomy")
        self.assertEqual(body["season"], 11)
        self.assertEqual(self.downloads(), [])
        await self.worker.handle(self.message("bot oui"))
        self.assertEqual(self.downloads()[0].kwargs["json"]["prefs"]["season"], 11)

    async def test_chat_is_scoped_and_does_not_execute(self):
        self.manager.request.side_effect = [
            {"route": {"action": "chat", "request": "bonjour"}}, {"result": "Bonjour !"},
            {"route": {"action": "chat", "request": "discussion"}}, {"result": "Oui."},
            {"route": {"action": "chat", "request": "bonjour"}}, {"result": "Salut."}]
        await self.worker.handle(self.message("bot bonjour"))
        await self.worker.handle(self.message("bot tu te rappelles ?", message_id=2001))
        history = self.manager.request.await_args_list[2].kwargs["json"]["messages"]
        self.assertIn({"role": "assistant", "content": "Bonjour !"}, history)
        await self.worker.handle(self.message("bot bonjour", user=43))
        other = self.manager.request.await_args_list[4].kwargs["json"]["messages"]
        self.assertEqual(len(other), 1)
        self.assertEqual(self.plex.request.await_count, 0)

    async def test_download_question_fetches_own_live_facts(self):
        self.manager.request.side_effect = [
            {"route": {"action": "downloads", "request": "dernier bloqué"}}, {"result": "Aucun seed connecté."}]
        self.plex.request.side_effect = None
        self.plex.request.return_value = {"downloads": [{"title": "Film", "num_seeds": 0, "observation": "Aucun seed connecté"}]}
        await self.worker.handle(self.message("bot pourquoi mon dernier téléchargement est bloqué ?"))
        self.assertEqual(self.plex.request.await_args.args, ("GET", "/downloads/status"))
        self.assertEqual(self.plex.request.await_args.kwargs["params"], {"user_id": 42})
        prompt = self.manager.request.await_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn('"num_seeds": 0', prompt)
        self.assertEqual(self.downloads(), [])

    async def test_service_status_keeps_admin_permission(self):
        self.manager.request.return_value = {"route": {"action": "services", "request": "état serveur"}}
        await self.worker.handle(self.message("bot le serveur fonctionne ?"))
        self.assertEqual(self.manager.request.await_count, 1)
        self.assertIn("administrateurs", self.channel.send.await_args.args[0])

    async def test_unprefixed_messages_without_pending_request_are_ignored(self):
        await self.worker.handle(self.message("bonjour tout le monde"))
        await self.worker.handle(self.message("botanique"))
        self.assertEqual(self.manager.request.await_count, 0)

    async def test_search_refinement_uses_history_and_reconfirms(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        self.manager.request.side_effect = [
            {"route": {"action": "search", "request": "Charlie de 2005 en français"}}, {"intent": self.intent}]
        await self.worker.handle(self.message("plutôt en français", message_id=2001))
        routed = self.manager.request.await_args_list[1].kwargs["json"]["messages"]
        self.assertTrue(any("Charlie" in m["content"] for m in routed))
        self.assertEqual(self.downloads(), [])

    async def test_chat_does_not_discard_pending_confirmation(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        self.manager.request.side_effect = [
            {"route": {"action": "chat", "request": "1080p ?"}}, {"result": "Une résolution."}]
        await self.worker.handle(self.message("bot c’est quoi 1080p ?"))
        await self.worker.handle(self.message("oui", message_id=2001))
        self.assertEqual(len(self.downloads()), 1)

    async def test_download_facts_still_displayed_if_chat_model_fails(self):
        self.manager.request.side_effect = [
            {"route": {"action": "downloads", "request": "dernier"}}, TimeoutError()]
        self.plex.request.side_effect = None
        self.plex.request.return_value = {"downloads": [{"title": "Film", "observation": "Téléchargement en pause."}]}
        await self.worker.handle(self.message("bot mon téléchargement ?"))
        self.assertIn("Téléchargement en pause", self.channel.send.await_args.args[0])

    async def test_expired_history_is_not_sent_to_model(self):
        self.worker.remember("conversation:1:123:42", "assistant", "Ancienne conversation")
        self.now = 1801
        self.manager.request.side_effect = [
            {"route": {"action": "chat", "request": "salut"}}, {"result": "Salut !"}]
        await self.worker.handle(self.message("bot salut"))
        self.assertEqual(len(self.manager.request.await_args_list[0].kwargs["json"]["messages"]), 1)

    async def test_new_search_cannot_replace_uncertain_add(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        self.plex.request.side_effect = TimeoutError()
        await self.worker.handle(self.message("oui"))
        calls = self.manager.request.await_count
        await self.worker.handle(self.message("bot cherche Dune", message_id=2001))
        self.assertEqual(self.manager.request.await_count, calls)
        self.assertEqual(len(self.downloads()), 1)

    async def test_seed_request_is_grounded_in_original_message_not_route_guesses(self):
        self.manager.request.side_effect = [
            {"route": {"action": "search", "request": "Grey’s Anatomy S11 de 2010 en français en 1080p"}},
            {"intent": {**self.intent, "title": "Grey’s Anatomy", "year": 2010, "season": 11}}]
        self.items[:] = [{"title": "Grey’s Anatomy S11 Complete", "seeders": "9", "enclosure": "result:first"}]
        with patch.dict(os.environ, {"SEARCH_DEFAULT_QUALITY": "720p"}):
            await self.worker.handle(self.message("bot tu pourrais me trouver une version de greys anatomy S11 qui a des seed stp", message_id=100))
        body = self.plex.request.await_args.kwargs["json"]
        self.assertIsNone(body["year"])
        self.assertEqual(body["quality"], "720p")
        self.assertIsNone(body["language"])
        self.assertEqual(body["season"], 11)
        self.assertEqual(body["min_seeders"], 1)
        self.assertTrue(body["strict_series"])
        state = self.store.get("conversation:1:123:42")
        self.assertEqual(state["prefs"]["target_name"], "Grey’s Anatomy")
        self.assertIn("**9**", self.channel.send.await_args.args[0])
        self.assertEqual(self.downloads(), [])

    async def test_no_positive_seed_result_does_not_create_proposal(self):
        self.plex.request.side_effect = None
        self.plex.request.return_value = {"items": [], "errors": []}
        await self.worker.handle(self.message("bot cherche Charlie avec des seeds", message_id=100))
        self.assertIn("aucun torrent compatible", self.channel.send.await_args.args[0].lower())
        self.assertEqual(self.store.get("conversation:1:123:42")["phase"], "retry")
        await self.worker.handle(self.message("oui"))
        self.assertEqual(self.downloads(), [])

    async def test_quality_change_requires_separate_add_confirmation(self):
        self.plex.request.side_effect = [
            {"items": [], "errors": [], "quality_options": ["720p", "480p"]},
            {"items": self.items, "errors": []}, {"id": "task-id"}]
        await self.worker.handle(self.message("bot cherche Charlie de 2005 en 1080p", message_id=100))
        self.assertEqual(self.store.get("conversation:1:123:42")["phase"], "quality")
        await self.worker.handle(self.message("oui", message_id=101))
        self.assertEqual(self.plex.request.await_count, 1)  # Sent before proposal: no consent.
        await self.worker.handle(self.message("oui", message_id=2000))
        self.assertEqual(self.plex.request.await_args.kwargs["json"]["quality"], "720p")
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.store.get("conversation:1:123:42")["phase"], "confirm")
        await self.worker.handle(self.message("oui", message_id=2001))
        self.assertEqual(len(self.downloads()), 1)

    async def test_quality_offer_can_be_cancelled(self):
        self.plex.request.side_effect = None
        self.plex.request.return_value = {"items": [], "errors": [], "quality_options": ["720p"]}
        await self.worker.handle(self.message("bot cherche Charlie en 1080p", message_id=100))
        await self.worker.handle(self.message("non"))
        self.assertIsNone(self.store.get("conversation:1:123:42"))
        self.assertEqual(self.downloads(), [])

    async def test_default_quality_and_policy_apply_without_explicit_filters(self):
        with patch.dict(os.environ, {"SEARCH_DEFAULT_QUALITY": "1080p"}):
            await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        body = self.plex.request.await_args.kwargs["json"]
        self.assertEqual(body["quality"], "1080p")
        self.assertTrue(body["selection_policy"])
        self.assertIsNone(body["min_seeders"])

    async def test_man_alias_search_and_confirmation(self):
        await self.worker.handle(self.message("Man, cherche Charlie de 2005", message_id=100))
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.manager.request.await_count, 1)
        await self.worker.handle(self.message("man oui"))
        self.assertEqual(len(self.downloads()), 1)

    async def test_man_alias_chat_and_word_boundary(self):
        self.manager.request.side_effect = [
            {"route": {"action": "chat", "request": "bonjour"}}, {"result": "Bonjour !"}]
        await self.worker.handle(self.message("man bonjour"))
        self.assertEqual(self.channel.send.await_args.args[0], "Bonjour !")
        await self.worker.handle(self.message("manger du chocolat"))
        self.assertEqual(self.manager.request.await_count, 2)

    async def test_zero_seed_warning_shown_before_and_after_confirmed_add(self):
        self.items[:] = [{'title': 'Charlie.2005.MULTI.1080p', 'enclosure': 'result:first', 'seeders': '0',
                         'availability_warning': 'Aucun seed annoncé. Cela peut prendre beaucoup de temps.'}]
        await self.worker.handle(self.message('bot cherche Charlie de 2005', message_id=100))
        self.assertIn('beaucoup de temps', self.channel.send.await_args.args[0])
        self.assertIn('Réponds **oui**', self.channel.send.await_args.args[0])
        self.assertEqual(self.downloads(), [])
        await self.worker.handle(self.message('oui'))
        self.assertEqual(len(self.downloads()), 1)
        self.assertIn('beaucoup de temps', self.channel.send.await_args.args[0])


    async def test_sf_inventory_escapes_old_clarification_without_searching(self):
        key = "conversation:1:123:42"
        self.worker.save(key, {"phase": "clarify", "request": "SF", "user_request": "SF"})
        self.plex.request.side_effect = None
        self.plex.request.return_value = {"items": [{"title": "The Expanse", "year": 2015, "genres": ["Science-fiction"], "summary": "Une aventure spatiale."}], "truncated": False}
        await self.worker.handle(self.message("man quelle série de SF est disponible sur le plex ?"))
        self.manager.request.assert_not_awaited()
        self.assertEqual(self.plex.request.await_args.args[1], "/catalog/library")
        self.assertIn("The Expanse", self.channel.send.await_args.args[0])
        self.assertIsNone(self.store.get(key))
        self.assertEqual(self.downloads(), [])

    async def test_tmdb_choice_keeps_season_and_requires_distinct_add_confirmation(self):
        media = {"title": "Charlie", "kind": "series", "year": 2026, "tmdb_id": 123, "imdb_id": "tt123", "aliases": ["Charlie", "Original"], "tmdb_url": "https://www.themoviedb.org/tv/123"}
        self.manager.request.side_effect = [{"intent": {**self.intent, "kind": "series", "season": 1}}, {"items": [media], "selected": None}, media]
        with patch.dict(os.environ, {"TMDB_ACCESS_TOKEN": "test-token"}):
            await self.worker.handle(self.message("bot cherche Charlie saison 1", message_id=100))
            self.assertEqual(self.plex.request.await_count, 0)
            await self.worker.handle(self.message("oui"))
            self.assertEqual(self.plex.request.await_count, 0)
            await self.worker.handle(self.message("1", message_id=2001))
            body = self.plex.request.await_args.kwargs["json"]
            self.assertEqual(body["season"], 1)
            self.assertEqual(body["imdb_id"], "tt123")
            self.assertEqual(body["query_aliases"], ["Charlie", "Original"])
            self.assertEqual(self.downloads(), [])
            await self.worker.handle(self.message("oui", message_id=2002))
            self.assertEqual(len(self.downloads()), 1)

    async def test_refine_reuses_identification_and_requires_new_confirmation(self):
        await self.worker.handle(self.message("bot cherche Charlie de 2005", message_id=100))
        self.manager.request.reset_mock()
        await self.worker.handle(self.message("man ce n’est pas le bon", message_id=2001))
        self.manager.request.assert_not_awaited()
        self.assertTrue(self.plex.request.await_args.kwargs['json']['refined'])
        self.assertEqual(self.downloads(), [])
        state = self.store.get('conversation:1:123:42')
        await self.worker.handle(self.message('oui', message_id=2002, reference=SimpleNamespace(message_id=1002)))
        self.assertEqual(self.downloads(), [])
        await self.worker.handle(self.message('oui', message_id=2003, reference=SimpleNamespace(message_id=state['proposal_message_id'])))
        self.assertEqual(len(self.downloads()), 1)
