import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from chamoxis_common.store import Store, Conflict
from shared.schemas import AddTorrent, Preferences, Event, MediaIntent
from modules.plex.core.downloads import Downloads
from modules.plex.api.server import create_app as plex_app
from manager.api.server import create_app as manager_app
from manager.core.service_registry import Service, Registry
from manager.core.supervisor import Supervisor
from manager.ai.router import Router, AIUnavailable
from manager.ai.gateway import Gateway
from manager.core.permissions import administrator, allowed_user
from manager.core.notifications import Notifications


def download(request_id="one"):
    return AddTorrent(request_id=request_id, link="magnet:?xt=urn:btih:" + "a" * 40,
                      title="Film", user_id=42, channel_id=123, confirmed=True).model_dump()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_idempotence_and_conflicting_payload(self):
        first = self.store.create_task("one", download())
        self.assertEqual(first["id"], self.store.create_task("one", download())["id"])
        other = download()
        other["user_id"] = 43
        with self.assertRaises(Conflict):
            self.store.create_task("one", other)

    def test_persistent_outbox_and_task_reopen(self):
        task = self.store.create_task("one", download())
        event = Event(source="plex", type="torrent.added")
        self.assertTrue(self.store.event(event))
        self.assertFalse(self.store.event(event))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.task(task["id"])["state"], "queued")
        self.assertEqual(self.store.pending_events()[0]["id"], event.id)
        self.store.delivered(event.id)
        self.assertEqual(self.store.pending_events(), [])

    def test_atomic_claim(self):
        task = self.store.create_task("one", download())
        self.assertTrue(self.store.claim(task["id"], "queued", "adding"))
        self.assertFalse(self.store.claim(task["id"], "queued", "adding"))

    def test_path_validation_and_permissions(self):
        for bad in ["../movie", "C:\\media", ".."]:
            with self.assertRaises(ValueError):
                Preferences(target_name=bad)
        with patch.dict(os.environ, {"DISCORD_ADMIN_IDS": "1", "DISCORD_ALLOWED_USER_IDS": "42"}):
            self.assertTrue(administrator(1))
            self.assertFalse(administrator(42))
            self.assertTrue(allowed_user(42))
            self.assertFalse(allowed_user(99))


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite3")
        qbit = AsyncMock()
        self.engine = SimpleNamespace(qbit=qbit, DISABLE_TORRENT_DOWNLOAD=False,
                                     TORZNAB_FORCE_UPLOAD=False, PLEX_MOVIES_SECTION_ID="1", PLEX_SERIES_SECTION_ID="2",
                                     hashes_from_torrents=lambda rows: {t["hash"] for t in rows},
                                     parse_info_hash_from_magnet=lambda value: "a" * 40,
                                     add_torrent_from_link=AsyncMock(), import_torrent_entry=AsyncMock(),
                                     plex_refresh=AsyncMock(return_value=(True, "OK")))
        self.worker = Downloads(self.store, self.engine)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_download_and_import_without_manager_or_discord(self):
        task = self.store.create_task("one", download())
        self.engine.qbit.list_torrents.side_effect = [[], [{"hash": "a" * 40, "name": "Film"}]]
        await self.worker.step()
        self.assertEqual(self.store.task(task["id"])["state"], "downloading")
        self.engine.qbit.get_torrent_by_hash.return_value = {"hash": "a" * 40, "progress": 1}
        self.engine.import_torrent_entry.return_value = ("Import OK", False, True, 1, "a" * 40)
        await self.worker.step()
        self.assertEqual(self.store.task(task["id"])["state"], "completed")
        self.engine.import_torrent_entry.assert_awaited_once()
        self.assertIn("plex.import.completed", {e["type"] for e in self.store.pending_events()})
        await self.worker.step()
        self.engine.import_torrent_entry.assert_awaited_once()

    async def test_poll_failure_keeps_download_for_retry(self):
        task = self.store.create_task("one", download())
        self.store.transition(task["id"], "downloading", {"hash": "a" * 40})
        self.engine.qbit.get_torrent_by_hash.side_effect = ConnectionError("offline")
        await self.worker.step()
        self.assertEqual(self.store.task(task["id"])["state"], "downloading")

    async def test_crash_recovery_does_not_replay_side_effects(self):
        for key, state in [("one", "adding"), ("two", "importing")]:
            task = self.store.create_task(key, download(key))
            self.store.transition(task["id"], state)
        self.worker.recover()
        await self.worker.step()
        self.assertEqual({t["state"] for t in self.store.tasks()}, {"needs_review"})
        self.engine.add_torrent_from_link.assert_not_awaited()
        self.engine.import_torrent_entry.assert_not_awaited()

    async def test_upload_state_is_not_completion(self):
        task = self.store.create_task("one", download())
        self.store.transition(task["id"], "downloading", {"hash": "a" * 40})
        self.engine.qbit.get_torrent_by_hash.return_value = {"progress": .9, "state": "uploading"}
        await self.worker.step()
        self.engine.import_torrent_entry.assert_not_awaited()

    async def test_cancellation_during_poll_prevents_import(self):
        task = self.store.create_task("one", download())
        self.store.transition(task["id"], "downloading", {"hash": "a" * 40})
        async def cancelled_during_request(*args):
            self.store.transition(task["id"], "cancelled")
            return {"progress": 1}
        self.engine.qbit.get_torrent_by_hash.side_effect = cancelled_during_request
        await self.worker.step()
        self.assertEqual(self.store.task(task["id"])["state"], "cancelled")
        self.engine.import_torrent_entry.assert_not_awaited()

    async def test_test_mode_never_calls_qbit(self):
        task = self.store.create_task("one", download())
        self.engine.DISABLE_TORRENT_DOWNLOAD = True
        await self.worker.step()
        self.assertEqual(self.store.task(task["id"])["state"], "simulated")
        self.engine.qbit.list_torrents.assert_not_awaited()


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MANAGER_API_KEY": "module-key", "MANAGER_ADMIN_API_KEY": "admin-key",
                                         "PLEX_MODULE_API_KEY": "plex-key", "DISCORD_NOTIFICATION_CHANNEL_IDS": "123"})
        self.env.start()
        self.manager_store = Store(Path(self.tmp.name) / "manager.sqlite3")
        self.plex_store = Store(Path(self.tmp.name) / "plex.sqlite3")
        self.manager = manager_app(self.manager_store, Registry([]), background=False)
        self.engine = SimpleNamespace(qbit=AsyncMock(), INDEXER_IDS=["1"], INDEXER_LABELS={},
                                     DISABLE_TORRENT_DOWNLOAD=True, PROWLARR_API_KEY="configured")
        self.plex = plex_app(self.engine, self.plex_store, background=False)

    async def asyncTearDown(self):
        self.manager_store.close()
        self.plex_store.close()
        self.env.stop()
        self.tmp.cleanup()

    async def test_authentication_and_administrative_key_separation(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.manager), base_url="http://manager") as client:
            self.assertEqual((await client.get("/health")).status_code, 401)
            self.assertEqual((await client.get("/health", headers={"X-API-Key": "module-key"})).status_code, 200)
            self.assertEqual((await client.post("/services/test/restart", headers={"X-API-Key": "module-key"})).status_code, 401)
            self.assertEqual((await client.post("/services/test/restart", headers={"X-API-Key": "admin-key"})).status_code, 404)

    async def test_duplicate_events_and_forbidden_channel(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.manager), base_url="http://manager",
                                     headers={"X-API-Key": "module-key"}) as client:
            event = Event(source="plex", type="torrent.added", channel_id=123).model_dump()
            self.assertFalse((await client.post("/events", json=event)).json()["duplicate"])
            self.assertTrue((await client.post("/events", json=event)).json()["duplicate"])
            event["channel_id"] = 999
            self.assertEqual((await client.post("/events", json=event)).status_code, 403)

    async def test_plex_add_contract_and_idempotence(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.plex), base_url="http://plex",
                                     headers={"X-API-Key": "plex-key"}) as client:
            self.assertEqual((await client.get("/health", headers={"X-API-Key": "wrong"})).status_code, 401)
            first = await client.post("/downloads", json=download())
            second = await client.post("/downloads", json=download())
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()["id"], second.json()["id"])
            bad = download()
            bad["confirmed"] = False
            self.assertEqual((await client.post("/downloads", json=bad)).status_code, 422)
            bad = download()
            bad["user_id"] = 55
            self.assertEqual((await client.post("/downloads", json=bad)).status_code, 409)

    async def test_module_registration_requires_configuration(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.manager), base_url="http://manager",
                                     headers={"X-API-Key": "module-key"}) as client:
            response = await client.post("/modules/register", json={"id": "arbitrary", "url": "http://127.0.0.1:9876"})
            self.assertEqual(response.status_code, 403)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def supervisor(self, services):
        self.now = 0
        driver = SimpleNamespace(health=AsyncMock(return_value=(False, "process_stopped")),
                                 start=AsyncMock(), stop=AsyncMock())
        sup = Supervisor(Registry(services), driver=driver, clock=lambda: self.now)
        return sup, driver

    async def test_backoff_and_maximum_attempts(self):
        service = Service(id="test", name="Test", type="python", command=["python", "worker.py"],
                          desired="running", restart=True, external_supervisor=False,
                          startup_delay=0, interval=1, max_attempts=2, backoff=10)
        sup, driver = self.supervisor([service])
        await sup.tick()
        self.now = 1
        await sup.tick()
        driver.start.assert_awaited_once()
        self.now = 10
        await sup.tick()
        self.now = 100
        await sup.tick()
        self.assertEqual(driver.start.await_count, 2)
        self.assertEqual(sup.states["test"]["status"], "restart_limit")

    async def test_monitor_and_disabled_never_restart(self):
        services = [Service(id=state, name=state, desired=state, health_url="http://local/health", startup_delay=0)
                    for state in ["monitor", "stopped"]]
        sup, driver = self.supervisor(services)
        await sup.tick()
        driver.start.assert_not_awaited()
        self.assertEqual(sup.states["stopped"]["status"], "disabled")
        with self.assertRaises(ValueError):
            await sup.restart("monitor")

    async def test_startup_grace_and_dependencies(self):
        service = Service(id="test", name="Test", startup_delay=30, health_url="http://local/health")
        sup, driver = self.supervisor([service])
        await sup.tick()
        self.assertEqual(sup.states["test"]["status"], "starting")
        driver.health.assert_not_awaited()
        self.now = 31
        await sup.tick()
        driver.health.assert_awaited_once()

    def test_reject_double_supervision_and_cycles(self):
        with self.assertRaises(ValueError):
            Service(id="test", name="Test", restart=True)
        with self.assertRaises(ValueError):
            Registry([Service(id="a", name="A", depends_on=["b"]), Service(id="b", name="B", depends_on=["a"])])


class AITests(unittest.IsolatedAsyncioTestCase):
    def backend(self, available=True, result='{"title":"Dune","year":2021}'):
        return SimpleNamespace(url="http://ollama", model="qwen3:4b", available=AsyncMock(return_value=available),
                               chat=AsyncMock(return_value=result))

    async def test_remote_priority_and_local_fallback(self):
        remote, local = self.backend(), self.backend()
        remote.chat.side_effect = TimeoutError()
        result = await Router([remote, local]).generate([], validate=MediaIntent.model_validate_json)
        self.assertEqual(result["result"].title, "Dune")
        local.chat.assert_awaited_once()

    async def test_invalid_json_falls_back(self):
        remote, local = self.backend(result='{"title":"Dune", "system_command":"del files"}'), self.backend()
        response = await Gateway(Router([remote, local])).analyze("Ajoute Dune 2021")
        self.assertEqual(response["intent"]["year"], 2021)
        local.chat.assert_awaited_once()

    async def test_both_models_unavailable(self):
        remote, local = self.backend(False), self.backend(False)
        with self.assertRaises(AIUnavailable):
            await Router([remote, local]).generate([])
        remote.chat.assert_not_awaited()
        local.chat.assert_not_awaited()

    async def test_remote_success_does_not_use_local(self):
        remote, local = self.backend(), self.backend()
        await Router([remote, local]).generate([])
        local.chat.assert_not_awaited()


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_deduplication_and_offline_retention(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DISCORD_NOTIFICATION_CHANNEL_IDS": "123"}):
            store = Store(Path(tmp) / "state.sqlite3")
            event = Event(source="plex", type="plex.import.completed", channel_id=123)
            store.event(event)
            channel = SimpleNamespace(send=AsyncMock())
            bot = SimpleNamespace(is_ready=lambda: False, get_channel=lambda _: channel)
            worker = Notifications(store, bot)
            await worker.step()
            channel.send.assert_not_awaited()
            store.db.execute("UPDATE events SET next_attempt=0")
            store.db.commit()
            bot.is_ready = lambda: True
            await worker.step()
            store.event(event)
            await worker.step()
            channel.send.assert_awaited_once()
            store.close()
