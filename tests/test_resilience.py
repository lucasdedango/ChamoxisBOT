import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_tmp = tempfile.mkdtemp()
os.environ.setdefault("PLEX_MOVIES_PATHS", _tmp)
os.environ.setdefault("PLEX_SERIES_PATHS", _tmp)
os.environ.setdefault("BOT_LOG_FILE", os.path.join(_tmp, "bot.log"))

import bot


class RedactionTests(unittest.TestCase):
    def test_redacts_url_and_mapping_secrets(self):
        url = "http://localhost/api?q=Silo&apikey=secret-token&limit=20"
        self.assertEqual(
            bot.redact_sensitive(url),
            "http://localhost/api?q=Silo&apikey=***REDACTED***&limit=20",
        )
        self.assertEqual(
            bot.redact_sensitive({"Authorization": "Bearer abc", "query": "Silo"}),
            {"Authorization": "***REDACTED***", "query": "Silo"},
        )

    def test_redacts_complete_bearer_authorization_header(self):
        redacted = bot.redact_sensitive("Authorization: Bearer abc123")
        self.assertEqual(redacted, "Authorization: ***REDACTED***")
        self.assertNotIn("abc123", redacted)


class RenameTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_root_folder_keeps_authoritative_qbit_prefix(self):
        fake = AsyncMock()
        fake.get_torrent_by_hash.return_value = {
            "save_path": r"D:\downloads",
            "content_path": r"D:\downloads\Release.Name",
        }
        fake.list_files.return_value = [{"name": "Release.Name/episode.mkv"}]
        with patch.object(bot, "qbit", fake):
            await bot.rename_file_resilient(
                "abc", Path("episode.mkv"), Path("Show - S01E01.mkv")
            )
        fake.rename_file.assert_awaited_once_with(
            "abc",
            Path("Release.Name/episode.mkv"),
            Path("Release.Name/Show - S01E01.mkv"),
        )

    async def test_already_renamed_is_idempotent(self):
        fake = AsyncMock()
        fake.get_torrent_by_hash.return_value = {"state": "uploading"}
        fake.list_files.return_value = [{"name": "S01/Show - S01E01.mkv"}]
        with patch.object(bot, "qbit", fake):
            await bot.rename_file_resilient(
                "abc", Path("old.mkv"), Path("S01/Show - S01E01.mkv")
            )
        fake.rename_file.assert_not_awaited()

    async def test_409_preserves_real_context(self):
        fake = AsyncMock()
        fake.get_torrent_by_hash.return_value = {
            "state": "pausedUP",
            "save_path": "/downloads",
            "content_path": "/downloads/release",
        }
        fake.list_files.return_value = [{"name": "release.mkv"}]
        fake.rename_file.side_effect = bot.QbitApiError(409, "/renameFile", "destination exists")
        with patch.object(bot, "qbit", fake), self.assertLogs("chamoxisbot", "ERROR") as logs:
            with self.assertRaisesRegex(RuntimeError, "destination exists"):
                await bot.rename_file_resilient(
                    "deadbeef", Path("release.mkv"), Path("Movie.mkv")
                )
        diagnostic = "\n".join(logs.output)
        self.assertIn("deadbeef", diagnostic)
        self.assertIn("pausedUP", diagnostic)
        self.assertIn("destination exists", diagnostic)

    async def test_move_waits_for_qbittorrent_to_finish(self):
        fake = AsyncMock()
        fake.get_torrent_by_hash.side_effect = [
            {"state": "moving", "save_path": "/old"},
            {"state": "uploading", "save_path": "/library/Show"},
        ]
        with patch.object(bot, "qbit", fake), patch.object(
            bot.asyncio, "sleep", AsyncMock()
        ):
            fresh = await bot.move_torrent_and_wait(
                "abc", Path("/library/Show"), timeout=1
            )
        self.assertEqual(fresh["state"], "uploading")
        fake.set_location.assert_awaited_once_with("abc", Path("/library/Show"))
        fake.list_files.assert_awaited_once_with("abc")


class CommandTests(unittest.TestCase):
    def test_registered_commands_exclude_removed_legacy_commands(self):
        names = {command.name for command in bot.bot.tree.get_commands()}
        self.assertEqual(
            names,
            {"status", "rssfeed", "recherchetorrent", "addtorrent", "cleartorrents", "info"},
        )


class BackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_task_exception_is_consumed_and_logged(self):
        async def fail():
            raise ValueError("boom")

        with self.assertLogs("chamoxisbot", "ERROR") as logs:
            task = bot.spawn_background(fail(), name="test-failure")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertTrue(task.done())
        self.assertIn("test-failure", "\n".join(logs.output))


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_temporary_qbit_error_does_not_stop_tracking(self):
        fake_qbit = AsyncMock()
        fake_qbit.get_torrent_by_hash.side_effect = [ConnectionError("offline"), None]
        interaction = AsyncMock()
        interaction.user.id = 42
        with patch.object(bot, "qbit", fake_qbit), patch.object(
            bot.asyncio, "sleep", AsyncMock()
        ) as sleep, patch.object(bot, "safe_edit_progress", AsyncMock()) as edit:
            await bot.track_download_progress(interaction, "abc", "Release", None)

        self.assertEqual(fake_qbit.get_torrent_by_hash.await_count, 2)
        sleep.assert_awaited_once_with(60)
        self.assertIn("nouvelle tentative", edit.await_args_list[0].kwargs["content"])


if __name__ == "__main__":
    unittest.main()
