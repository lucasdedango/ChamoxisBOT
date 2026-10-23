import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from chamoxis_common.store import Store
from manager.ai.gateway import Gateway
from manager.ai.router import Router
from manager.api.server import create_app as manager_app
from manager.core.service_registry import Registry
from modules.plex.api.server import create_app as plex_app
from shared.schemas import AddTorrent


class ConversationAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_route_schema_and_authenticated_endpoint(self):
        backend = SimpleNamespace(url="http://local", model="test", available=AsyncMock(return_value=True),
            chat=AsyncMock(return_value=json.dumps({"action": "search", "request": "Grey’s Anatomy saison 11"})))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "MANAGER_API_KEY": "test-key", "MANAGER_ADMIN_API_KEY": "admin-key"}):
            store = Store(Path(tmp) / "db")
            try:
                app = manager_app(store, Registry([]), Gateway(Router([backend])), background=False)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    body = {"messages": [{"role": "user", "content": "tu peux me trouver greys anatomy S11?"}]}
                    self.assertEqual((await client.post("/ai/route", json=body)).status_code, 401)
                    result = await client.post("/ai/route", json=body, headers={"X-API-Key": "test-key"})
                    self.assertEqual(result.json()["route"]["action"], "search")
                    backend.chat.return_value = '{"action":"delete","request":"tout"}'
                    self.assertEqual((await client.post("/ai/route", json=body, headers={"X-API-Key": "test-key"})).status_code, 503)
            finally:
                store.close()

    async def test_diagnostics_are_live_scoped_sanitized_and_creation_ordered(self):
        qbit = SimpleNamespace(get_torrent_by_hash=AsyncMock(return_value={
            "state": "stalledDL", "progress": .4, "dlspeed": 0, "num_seeds": 0,
            "save_path": "private", "tracker": "secret", "magnet_uri": "secret"}))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PLEX_MODULE_API_KEY": "test-key"}):
            store = Store(Path(tmp) / "db")
            try:
                def add(name, user):
                    return store.create_task(name, AddTorrent(request_id=name, link="result:opaque", title=name,
                                                              user_id=user, confirmed=True).model_dump())
                old = add("old", 42)
                latest = add("latest", 42)
                add("other-user", 43)
                store.transition(latest["id"], "downloading", {"hash": "abc"})
                store.transition(old["id"], "simulated")  # Updated later, but not the latest request.
                app = plex_app(SimpleNamespace(qbit=qbit), store, background=False)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.get("/downloads/status", params={"user_id": 42}, headers={"X-API-Key": "test-key"})
                    self.assertEqual(response.status_code, 200)
                    rows = response.json()["downloads"]
                    self.assertEqual([r["title"] for r in rows], ["latest", "old"])
                    self.assertIn("aucun seed", rows[0]["observation"])
                    for private in ("private", "secret", "other-user", "result:opaque", "abc"):
                        self.assertNotIn(private, response.text)
                    qbit.get_torrent_by_hash.assert_awaited_once_with("abc")
            finally:
                store.close()

    def test_completed_checking_and_queued_torrents_are_not_diagnosed_as_seed_stalls(self):
        from modules.plex.core.diagnostics import snapshot
        task = {"id": "id", "payload": {"title": "Film"}, "state": "downloading"}
        for state, progress, word in [("stalledUP", 1, "terminé"), ("checkingDL", .4, "vérifie"), ("queuedDL", .4, "attente")]:
            with self.subTest(state=state):
                data = snapshot(task, {"state": state, "progress": progress, "dlspeed": 0, "num_seeds": 0})
                self.assertIn(word, data["observation"])
