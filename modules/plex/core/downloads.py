"""Durable download/import worker, independent of Discord and the manager."""
import asyncio
import logging
from shared.schemas import Event

logger = logging.getLogger(__name__)


class Downloads:
    def __init__(self, store, engine, poll_interval=5):
        self.store, self.engine = store, engine
        self.poll_interval = poll_interval

    def emit(self, task, name, data=None):
        payload = task["payload"]
        self.store.event(Event(source="plex", type=name, task_id=task["id"],
                               channel_id=payload.get("channel_id"), user_id=payload["user_id"],
                               data=data or {}))

    def recover(self):
        # An external side effect and a SQLite commit cannot be atomic.
        # Never blindly replay an uncertain add or a partially completed import.
        for task in self.store.tasks({"adding", "importing"}):
            self.store.transition(task["id"], "needs_review")
            self.emit(task, "torrent.failed", {"reason": "Interrupted operation requires manual review"})

    async def step(self):
        for task in self.store.tasks({"queued", "downloading"}):
            try:
                if task["state"] == "queued":
                    await self.add(task)
                else:
                    await self.monitor(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A polling outage must not lose the task or stop other downloads.
                logger.exception("Task operation failed: %s", task["id"])
                current = self.store.task(task["id"])
                if current["state"] in {"adding", "importing"}:
                    self.store.transition(task["id"], "needs_review")
                    self.emit(task, "plex.import.failed" if current["state"] == "importing" else "torrent.failed",
                              {"reason": "Operation uncertain; inspect Plex module logs"})

    async def add(self, task):
        if self.engine.DISABLE_TORRENT_DOWNLOAD:
            self.store.transition(task["id"], "simulated", {"mode": "no downloads or imports"})
            self.emit(task, "torrent.simulated")
            return
        payload = task["payload"]
        before = await self.engine.qbit.list_torrents(limit=10000)
        before_hashes = self.engine.hashes_from_torrents(before)
        if not self.store.claim(task["id"], "queued", "adding"):
            return
        category = payload["prefs"]["kind"]
        link = payload["link"]
        if link.startswith("result:"):
            link = self.store.get("search_result:" + link[7:])
            if not link:
                raise ValueError("Search result no longer available")
        if self.engine.TORZNAB_FORCE_UPLOAD and not link.startswith("magnet:?"):
            content = await self.engine.download_torrent_with_retry(link)
            await self.engine.qbit.add_torrent_file(content, category=category)
        else:
            await self.engine.add_torrent_from_link("magnet" if link.startswith("magnet:?") else "torrent_url", link, category)
        found = None
        wanted = self.engine.parse_info_hash_from_magnet(link) if link.startswith("magnet:?") else None
        for _ in range(6):
            recent = await self.engine.qbit.list_torrents(limit=10000)
            candidates = [t for t in recent if t.get("hash", "").lower() not in before_hashes]
            if wanted:
                found = next((t for t in recent if t.get("hash", "").lower() == wanted), None)
            elif len(candidates) == 1:
                found = candidates[0]
            else:
                # Avoid the old broad fallback that could import someone else's torrent.
                matching = [t for t in candidates if t.get("name") == payload["title"]]
                if len(matching) == 1:
                    found = matching[0]
            if found:
                break
            await asyncio.sleep(0.7)
        if not found:
            self.store.transition(task["id"], "needs_review")
            self.emit(task, "torrent.failed", {"reason": "Added torrent could not be identified safely"})
            return
        duplicate = next((t for t in self.store.tasks() if t["id"] != task["id"] and
                          (t["result"] or {}).get("hash") == found["hash"] and
                          t["state"] not in {"cancelled", "failed"}), None)
        if duplicate:
            self.store.transition(task["id"], "duplicate", {"original_task_id": duplicate["id"]})
            self.emit(task, "torrent.failed", {"reason": "Torrent already managed by another task"})
            return
        result = {"hash": found["hash"], "name": found.get("name", payload["title"]), "progress": 0}
        self.store.transition(task["id"], "downloading", result)
        self.emit(task, "torrent.added", result)

    async def monitor(self, task):
        info = await self.engine.qbit.get_torrent_by_hash(task["result"]["hash"])
        # A cancellation can arrive while the network request is outstanding.
        if self.store.task(task["id"])["state"] != "downloading":
            return
        if not info:
            self.store.transition(task["id"], "failed", {**task["result"], "reason": "Torrent removed"})
            self.emit(task, "torrent.failed", {"reason": "Torrent removed from qBittorrent"})
            return
        progress = float(info.get("progress", 0))
        result = {**task["result"], "progress": progress, "state": info.get("state"),
                  "dlspeed": info.get("dlspeed", 0), "eta": info.get("eta")}
        self.store.transition(task["id"], "downloading", result)
        # Completion depends on real progress, never on an upload-like state alone.
        if progress < 1:
            if int(progress * 10) > int(task["result"].get("progress", 0) * 10):
                self.emit(task, "torrent.progress", result)
            return
        self.emit(task, "torrent.completed", result)
        if not self.store.claim(task["id"], "downloading", "importing"):
            return
        self.emit(task, "plex.import.started", result)
        logs = []
        message, series, movies, count, _ = await self.engine.import_torrent_entry(info, logs, task["payload"]["prefs"])
        refresh = []
        for active, section in [(movies, self.engine.PLEX_MOVIES_SECTION_ID), (series, self.engine.PLEX_SERIES_SECTION_ID)]:
            if active and section:
                ok, _ = await self.engine.plex_refresh(section)
                refresh.append({"section": section, "ok": ok})
        result.update(message=message, moved_files=count, refresh=refresh)
        self.store.transition(task["id"], "completed", result)
        self.emit(task, "plex.import.completed", result)

    async def run(self):
        self.recover()
        while True:
            await self.step()
            await asyncio.sleep(self.poll_interval)
