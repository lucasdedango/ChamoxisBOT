"""Real loopback HTTP exchanges; all external services are local test doubles."""
import asyncio
import os
import socket
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
import uvicorn
from chamoxis_common.http import APIClient
from shared.schemas import Event
from chamoxis_common.store import Store
from manager.ai.gateway import Gateway
from manager.ai.ollama_client import OllamaClient
from manager.ai.router import Router
from manager.core.service_registry import Registry
from manager.api.server import create_app as manager_app
from modules.plex.api.server import create_app as plex_app
from modules.plex.core import engine, search


async def start_uvicorn(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(100):
        if server.started:
            return server, task, f"http://127.0.0.1:{port}"
        if task.done():
            await task
            raise RuntimeError("Test server failed to start")
        await asyncio.sleep(.02)
    server.should_exit = True
    await task
    raise TimeoutError("Test server startup timed out")


class NetworkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_analysis_and_durable_event_over_http(self):
        fake = web.Application()

        async def tags(request):
            return web.json_response({"models": [{"name": "qwen3:4b"}]})

        async def chat(request):
            body = await request.json()
            self.assertFalse(body["stream"])
            self.assertEqual(body["model"], "qwen3:4b")
            self.assertEqual(body["format"]["type"], "object")
            return web.json_response({"message": {"content": '{"title":"Dune","year":2021,"language":"français","quality":"1080p"}'}})

        async def torznab(request):
            self.assertEqual(request.query["q"], "Dune 2021")
            return web.Response(text='<rss><channel><item><title>Dune.2021.FRENCH.1080p</title><enclosure url="http://test.invalid/dune.torrent" length="12345"/></item></channel></rss>', content_type="application/xml")

        fake.router.add_get("/api/tags", tags)
        fake.router.add_post("/api/chat", chat)
        fake.router.add_get("/api/v1/indexer/1/newznab/", torznab)
        runner = web.AppRunner(fake)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        fake_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        servers = []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "MANAGER_API_KEY": "test-module-key", "MANAGER_ADMIN_API_KEY": "test-admin-key",
            "PLEX_MODULE_API_KEY": "test-plex-key", "DISCORD_NOTIFICATION_CHANNEL_IDS": "123",
        }):
            manager_store, plex_store = Store(Path(tmp) / "manager.sqlite3"), Store(Path(tmp) / "plex.sqlite3")
            try:
                gateway = Gateway(Router([OllamaClient(fake_url, "qwen3:4b")]))
                app = manager_app(manager_store, Registry([]), gateway=gateway, background=True)
                server, task, manager_url = await start_uvicorn(app)
                servers.append((server, task))
                with patch.dict(os.environ, {"MANAGER_URL": manager_url}), patch.object(search, "PROWLARR_URL", fake_url), patch.object(engine, "DISABLE_TORRENT_DOWNLOAD", True), patch.object(engine, "INDEXER_IDS", ["1"]):
                    app = plex_app(engine, plex_store, background=True)
                    server, task, plex_url = await start_uvicorn(app)
                    servers.append((server, task))
                    manager = APIClient(manager_url, "test-module-key")
                    plex = APIClient(plex_url, "test-plex-key")
                    analysis = await manager.request("POST", "/ai/analyze", json={"text": "Ajoute Dune 2021 en français en 1080p"})
                    self.assertEqual(analysis["intent"]["year"], 2021)
                    results = await plex.request("POST", "/search", json={"query": "Dune 2021", "indexer": "1", "language": "français", "quality": "1080p"})
                    self.assertEqual(len(results["items"]), 1)
                    task_data = await plex.request("POST", "/downloads", json={
                        "request_id": "loopback-request", "link": results["items"][0]["enclosure"],
                        "title": results["items"][0]["title"], "user_id": 42, "channel_id": 123,
                        "prefs": {"kind": "movies", "target_name": "Dune (2021)"}, "confirmed": True})
                    for _ in range(100):
                        current = await plex.request("GET", "/tasks/" + task_data["id"])
                        if current["state"] == "simulated":
                            break
                        await asyncio.sleep(.1)
                    self.assertEqual(current["state"], "simulated")
                    event = Event(source="plex", type="torrent.added", channel_id=123)
                    plex_store.event(event)
                    for _ in range(60):
                        if any(e["id"] == event.id for e in manager_store.pending_events()):
                            break
                        await asyncio.sleep(.1)
                    self.assertTrue(any(e["id"] == event.id for e in manager_store.pending_events()))
                    reply = await manager.request("POST", "/events", json=event.model_dump())
                    self.assertTrue(reply["duplicate"])
            finally:
                for server, task in reversed(servers):
                    server.should_exit = True
                    await task
                manager_store.close()
                plex_store.close()
                await runner.cleanup()

    async def test_ollama_model_missing_and_generation_serialization(self):
        active, maximum = 0, 0
        fake = web.Application()

        async def tags(request):
            return web.json_response({"models": [{"name": "other:model"}]})

        async def chat(request):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            await asyncio.sleep(.02)
            active -= 1
            return web.json_response({"message": {"content": "ok"}})

        fake.router.add_get("/api/tags", tags)
        fake.router.add_post("/api/chat", chat)
        runner = web.AppRunner(fake)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
            client = OllamaClient(url, "qwen3:4b")
            self.assertFalse(await client.available())
            await asyncio.gather(client.chat([]), client.chat([]))
            self.assertEqual(maximum, 1)
        finally:
            await runner.cleanup()
