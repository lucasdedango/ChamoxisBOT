"""Regression checks against the extracted engine, not the legacy entry point."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("PLEX_MOVIES_PATHS", _tmp.name)
os.environ.setdefault("PLEX_SERIES_PATHS", _tmp.name)
from modules.plex.core import engine


class ExtractedEngineTests(unittest.IsolatedAsyncioTestCase):
    def test_broad_ranking_keeps_alternatives_and_prefers_requested_edition(self):
        from modules.plex.core.search import rank_results
        items = [{"title": "Charlie.et.la.Chocolaterie.1971.FRENCH.1080p", "seeders": "100"},
                 {"title": "Charlie.et.la.Chocolaterie.2005.ENGLISH.720p", "seeders": "50"},
                 {"title": "Charlie.et.la.Chocolaterie.2005.MULTI.1080p", "seeders": "5"}]
        ranked = rank_results(items, "Charlie et la Chocolaterie", year=2005, quality="1080p", language="français")
        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["title"], items[2]["title"])
        self.assertEqual(ranked[-1]["title"], items[0]["title"])
        self.assertNotIn("preference_matches", items[0])

    def test_no_exact_quality_match_still_returns_results(self):
        from modules.plex.core.search import rank_results
        items = [{"title": "Dune.2021.720p"}, {"title": "Dune.2021.2160p"}]
        self.assertEqual(len(rank_results(items, "Dune", year=2021, quality="1080p", language="français")), 2)

    def test_series_episode_preferences_rank_after_broad_search(self):
        from modules.plex.core.search import rank_results
        items = [{"title": "Andor.S01E01.1080p"}, {"title": "Andor.S02E01.1080p"}]
        self.assertEqual(rank_results(items, "Andor", season=2, episode=1)[0]["title"], items[1]["title"])

    async def test_qbit_authoritative_prefix_preserved(self):
        fake = AsyncMock()
        fake.get_torrent_by_hash.return_value = {"save_path": r"D:\downloads", "content_path": r"D:\downloads\Release.Name"}
        fake.list_files.return_value = [{"name": "Release.Name/episode.mkv"}]
        with patch.object(engine, "qbit", fake):
            await engine.rename_file_resilient("abc", Path("episode.mkv"), Path("Show - S01E01.mkv"))
        fake.rename_file.assert_awaited_once_with("abc", Path("Release.Name/episode.mkv"), Path("Release.Name/Show - S01E01.mkv"))

    async def test_qbit_409_diagnostics_preserved(self):
        fake = AsyncMock()
        fake.get_torrent_by_hash.return_value = {"state": "pausedUP", "save_path": "/downloads", "content_path": "/downloads/release"}
        fake.list_files.return_value = [{"name": "release.mkv"}]
        fake.rename_file.side_effect = engine.QbitApiError(409, "/renameFile", "destination exists")
        with patch.object(engine, "qbit", fake), self.assertLogs("chamoxisbot.plex", "ERROR") as logs:
            with self.assertRaisesRegex(RuntimeError, "destination exists"):
                await engine.rename_file_resilient("deadbeef", Path("release.mkv"), Path("Movie.mkv"))
        self.assertIn("deadbeef", "\n".join(logs.output))

    async def test_completed_move_is_verified(self):
        fake = AsyncMock()
        fake.get_torrent_by_hash.side_effect = [{"state": "moving", "save_path": "/old"}, {"state": "uploading", "save_path": "/library/Show"}]
        with patch.object(engine, "qbit", fake), patch.object(engine.asyncio, "sleep", AsyncMock()):
            result = await engine.move_torrent_and_wait("abc", Path("/library/Show"), timeout=1)
        self.assertEqual(result["state"], "uploading")
        fake.list_files.assert_awaited_once_with("abc")

    def test_torznab_metadata_and_quality(self):
        xml = '<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item><title>Dune.2021.FRENCH.1080p</title><link>magnet:?xt=example</link><enclosure url="http://example/a.torrent" length="12345"/><torznab:attr name="seeders" value="7"/></item></channel></rss>'
        items = engine.parse_rss_feed(xml, source="C411")
        self.assertEqual(items[0]["seeders"], "7")
        self.assertEqual(items[0]["source"], "C411")
        self.assertTrue(engine.quality_matches(items[0]["title"], "1080p"))
        self.assertFalse(engine.quality_matches(items[0]["title"], "2160p"))

    async def test_movie_import_renames_and_moves_only_temporary_fixture(self):
        from modules.plex.core import storage
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads, library = root / "downloads", root / "library"
            downloads.mkdir()
            library.mkdir()
            video = downloads / "Dune.2021.1080p.mkv"
            video.write_bytes(b"temporary test fixture; not a real media file")
            info = {"hash": "abc", "name": video.name, "content_path": str(video), "save_path": str(downloads),
                    "progress": 1, "state": "uploading", "total_size": video.stat().st_size}
            fake = AsyncMock()
            fake.get_torrent_by_hash.side_effect = lambda _: dict(info)
            fake.list_files.side_effect = lambda _: [{"name": Path(info["content_path"]).name}]
            async def rename(info_hash, old, new):
                current = Path(info["content_path"])
                destination = current.parent / new
                current.rename(destination)
                info["content_path"] = str(destination)
            async def move(info_hash, destination):
                current = Path(info["content_path"])
                target = destination / current.name
                shutil.move(str(current), str(target))
                info.update(save_path=str(destination), content_path=str(target))
            fake.rename_file.side_effect, fake.set_location.side_effect = rename, move
            with patch.object(engine, "qbit", fake), patch.object(engine, "PLEX_MOVIES_PATHS", [library]), patch.object(storage, "MIN_FREE_BYTES", 0):
                message, series, movies, count, info_hash = await engine.import_torrent_entry(dict(info), [], {"kind": "movies", "target_name": "Dune (2021)"})
            self.assertTrue(movies)
            self.assertFalse(series)
            self.assertEqual(count, 1)
            self.assertTrue((library / "Dune (2021)" / "Dune (2021).mkv").is_file())
            self.assertFalse(video.exists())

    def test_engine_has_no_discord_dependencies(self):
        import ast
        for path in Path("modules/plex").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("discord", "manager")), str(path))
                elif isinstance(node, ast.Import):
                    self.assertFalse(any(n.name.startswith(("discord", "manager")) for n in node.names), str(path))

    def test_manager_has_no_plex_implementation_imports(self):
        import ast
        for path in Path("manager").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("modules"), str(path))
                elif isinstance(node, ast.Import):
                    self.assertFalse(any(n.name.startswith("modules") for n in node.names), str(path))
