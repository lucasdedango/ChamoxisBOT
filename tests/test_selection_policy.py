import os
import unittest
from unittest.mock import patch
from modules.plex.core.search import select_results, parse_rss_feed
from manager.ai.selection import default_quality


class SelectionPolicyTests(unittest.TestCase):
    def item(self, suffix, seeds="4", size="1000"):
        return {"title": "Greys.Anatomy.S11.Complete." + suffix, "seeders": seeds, "size": size}

    def select(self, items, **kwargs):
        return select_results(items, "Greys Anatomy", season=11, quality="1080p", **kwargs)

    def test_multi_then_availability_then_smallest_not_highest_seed_count(self):
        items = [self.item("FRENCH.1080p", "100", "100"),
                 self.item("MULTI.1080p", "50", "3000"),
                 self.item("MULTI.1080p", "3", "2000"),
                 self.item("MULTI.1080p", "", "500"),
                 self.item("MULTI.1080p.AV1", "20", "50"),
                 self.item("MULTI.1080p", "0", "10")]
        selected, _ = self.select(items)
        self.assertEqual([i["size"] for i in selected], ["2000", "3000", "500", "100"])
        self.assertIn("MULTI", selected[0]["selection_reason"])
        self.assertNotIn("selection_reason", items[0])

    def test_wrong_quality_season_title_and_episode_are_not_alternatives(self):
        items = [self.item("MULTI.1080p"), self.item("MULTI.720p"),
                 {"title": "Greys.Anatomy.S16.Complete.MULTI.1080p", "seeders": "100"},
                 {"title": "Greys.Anatomy.S11E01.MULTI.1080p", "seeders": "100"},
                 {"title": "Another.Show.S11.Complete.MULTI.1080p", "seeders": "100"}]
        selected, options = self.select(items)
        self.assertEqual(len(selected), 1)
        self.assertEqual(options, ["1080p", "720p"])

    def test_missing_quality_returns_options_without_selecting_other_quality(self):
        selected, options = self.select([self.item("MULTI.720p"), self.item("MULTI.480p")])
        self.assertEqual(selected, [])
        self.assertEqual(options, ["720p", "480p"])

    def test_unknown_size_is_not_treated_as_zero(self):
        selected, _ = self.select([self.item("MULTI.1080p", size=""), self.item("MULTI.1080p", size="123")])
        self.assertEqual(selected[0]["size"], "123")
        self.assertIn("taille inconnue", selected[1]["selection_reason"])

    def test_unknown_seed_is_possible_but_explicit_minimum_remains_strict(self):
        selected, _ = self.select([self.item("MULTI.1080p", seeds="")])
        self.assertIn("seeds : inconnus", selected[0]["selection_reason"])
        selected, options = self.select([self.item("MULTI.1080p", seeds="")], min_seeders=1)
        self.assertEqual((selected, options), ([], []))

    def test_explicit_language_overrides_multi_preference(self):
        selected, _ = self.select([self.item("MULTI.1080p", size="2000"),
                                   self.item("FRENCH.1080p", size="1000"),
                                   self.item("ENGLISH.1080p", size="50")], language="français")
        self.assertEqual([i["size"] for i in selected], ["1000", "2000"])

    def test_explicit_year_does_not_select_other_editions(self):
        items = [{"title": "Dune.1984.MULTI.1080p", "seeders": "100", "size": "10"},
                 {"title": "Dune.2021.MULTI.1080p", "seeders": "4", "size": "100"}]
        selected, _ = select_results(items, "Dune", quality="1080p", year=2021)
        self.assertEqual(selected[0]["title"], items[1]["title"])
        self.assertEqual(len(selected), 1)

    def test_torznab_size_overrides_torrent_file_enclosure_length(self):
        xml = '<rss xmlns:t="http://torznab.com/schemas/2015/feed"><channel><item><title>Film</title><enclosure length="12345"/><t:attr name="size" value="2000000000"/></item></channel></rss>'
        self.assertEqual(parse_rss_feed(xml)[0]["size"], "2000000000")

    def test_default_quality_is_configurable(self):
        with patch.dict(os.environ, {"SEARCH_DEFAULT_QUALITY": "720p"}):
            self.assertEqual(default_quality(), "720p")
        with patch.dict(os.environ, {"SEARCH_DEFAULT_QUALITY": "invalid"}):
            self.assertEqual(default_quality(), "1080p")


class SelectionPolicyAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_endpoint_and_quality_options_use_same_eligible_results(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import httpx
        from chamoxis_common.store import Store
        from modules.plex.api.server import create_app
        items = [{"title": "Show.S11.Complete.MULTI.720p", "seeders": "2", "size": "2000", "link": "https://example/a"},
                 {"title": "Show.S11.Complete.MULTI.720p", "seeders": "50", "size": "3000", "link": "https://example/b"},
                 {"title": "Show.S11.Complete.MULTI.1080p.AV1", "seeders": "8", "size": "100", "link": "https://example/c"},
                 {"title": "Show.S16.Complete.MULTI.1080p", "seeders": "9", "size": "200", "link": "https://example/d"}]
        engine = SimpleNamespace(INDEXER_IDS=["1"], build_torznab_search_url=lambda *a: "search",
                                 fetch_rss=AsyncMock(return_value="xml"), parse_rss_feed=lambda *a: items,
                                 indexer_label=lambda i: i, is_av1_title=lambda t: "AV1" in t)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PLEX_MODULE_API_KEY": "test-key"}):
            store = Store(Path(tmp) / "db")
            try:
                app = create_app(engine, store, background=False)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    body = {"query": "Show", "season": 11, "quality": "1080p", "selection_policy": True}
                    headers = {"X-API-Key": "test-key"}
                    result = (await client.post("/search", json=body, headers=headers)).json()
                    self.assertEqual(result["items"], [])
                    self.assertEqual(result["quality_options"], ["720p"])
                    body["quality"] = "720p"
                    result = (await client.post("/search", json=body, headers=headers)).json()
                    self.assertEqual([r["size"] for r in result["items"]], ["2000", "3000"])
                    self.assertIn("selection_reason", result["items"][0])
                    self.assertTrue(result["items"][0]["link"].startswith("result:"))
            finally:
                store.close()
