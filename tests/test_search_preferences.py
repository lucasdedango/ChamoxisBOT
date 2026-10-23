import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from chamoxis_common.store import Store
from manager.ai.preferences import explicit_preferences, ground_intent
from modules.plex.core.search import rank_results
from modules.plex.api.server import create_app


class SearchPreferencesTests(unittest.TestCase):
    def test_season_pack_with_seeds_excludes_other_seasons_episodes_and_unknowns(self):
        items = [{"title": "Greys.Anatomy.S11.Complete.720p", "seeders": "3"},
                 {"title": "Greys Anatomy Saison 11 Complete", "seeders": "10"},
                 {"title": "Greys.Anatomy.S11E01", "seeders": "100"},
                 {"title": "Greys.Anatomy.S16.Complete", "seeders": "900"},
                 {"title": "Greys.Anatomy.S22E07", "seeders": "1000"},
                 {"title": "Greys.Anatomy.S11.Complete", "seeders": "0"},
                 {"title": "Greys.Anatomy.S11.Complete"}]
        ranked = rank_results(items, "Greys Anatomy", season=11, strict_series=True, min_seeders=1)
        self.assertEqual([i["seeders"] for i in ranked], ["10", "3"])
        self.assertTrue(all(i["preference_matches"]["season"] for i in ranked))

    def test_episode_selection_excludes_packs_and_wrong_episode(self):
        items = [{"title": "Show.S11E02"}, {"title": "Show.S11E03"}, {"title": "Show.S11.Complete"},
                 {"title": "Show Saison 11 Episode 02"}]
        ranked = rank_results(items, "Show", season=11, episode=2, strict_series=True)
        self.assertEqual([i["title"] for i in ranked], [items[0]["title"], items[3]["title"]])

    def test_seed_minimum_unknown_and_zero_are_not_positive(self):
        items = [{"title": "Film", "seeders": s} for s in ("0", "", "?", "-1", "2", "12")]
        self.assertEqual([i["seeders"] for i in rank_results(items, "Film", min_seeders=5)], ["12"])

    def test_new_request_does_not_inherit_model_preferences(self):
        intent = {"title": "Grey’s Anatomy", "kind": "movies", "year": 2010, "quality": "1080p", "language": "français"}
        grounded = ground_intent(intent, "tu pourrais me trouver une version de greys anatomy S11 qui a des seed stp")
        self.assertIsNone(grounded["year"])
        self.assertIsNone(grounded["quality"])
        self.assertIsNone(grounded["language"])
        self.assertEqual(grounded["season"], 11)
        self.assertEqual(grounded["kind"], "series")
        self.assertEqual(grounded["min_seeders"], 1)

    def test_refinement_overrides_previous_explicit_preference(self):
        prefs = explicit_preferences("Charlie de 2005 en anglais en 1080p; précision : plutôt en français en 720p avec au moins 5 seeds")
        self.assertEqual(prefs["year"], 2005)
        self.assertEqual(prefs["quality"], "720p")
        self.assertEqual(prefs["language"], "français")
        self.assertEqual(prefs["min_seeders"], 5)


class SearchPreferencesAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_filters_broad_indexer_results(self):
        items = [{"title": "Show.S11.Complete", "seeders": "4", "link": "https://example/one"},
                 {"title": "Show.S16.Complete", "seeders": "400", "link": "https://example/two"},
                 {"title": "Show.S11.Complete.720p", "seeders": "0", "link": "https://example/three"}]
        engine = SimpleNamespace(INDEXER_IDS=["1"], build_torznab_search_url=lambda *a: "search",
                                 fetch_rss=AsyncMock(return_value="xml"), parse_rss_feed=lambda *a: items,
                                 indexer_label=lambda i: i, is_av1_title=lambda title: False)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PLEX_MODULE_API_KEY": "test-key"}):
            store = Store(Path(tmp) / "db")
            try:
                app = create_app(engine, store, background=False)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    result = await client.post("/search", headers={"X-API-Key": "test-key"}, json={
                        "query": "Show", "season": 11, "rank_preferences": True, "strict_series": True, "min_seeders": 1})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual([r["seeders"] for r in result.json()["items"]], ["4"])
                    self.assertTrue(result.json()["items"][0]["link"].startswith("result:"))
            finally:
                store.close()
