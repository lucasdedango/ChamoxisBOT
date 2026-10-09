import asyncio
import os
import shutil
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Query
from shared.schemas import AddTorrent, Search
from shared.schemas import Event
from chamoxis_common.security import require_key
from chamoxis_common.store import Conflict, Store
from modules.plex.core.downloads import Downloads
from modules.plex.bridge import deliver_events
from modules.plex.core.search import rank_results, language_matches
from modules.plex.core.diagnostics import snapshot

logger = logging.getLogger(__name__)


def create_app(engine=None, store=None, background=True):
    if engine is None:
        from modules.plex.core import engine
        for name, paths in [("PLEX_MOVIES_PATHS", engine.PLEX_MOVIES_PATHS), ("PLEX_SERIES_PATHS", engine.PLEX_SERIES_PATHS)]:
            if not paths or any(not path.is_dir() for path in paths):
                raise RuntimeError(f"{name} must reference existing directories on this machine")
    key = os.getenv("PLEX_MODULE_API_KEY", "")
    auth = require_key(key)
    store = store or Store(Path(os.getenv("PLEX_DATA_DIR", "data/plex")) / "state.sqlite3")
    worker = Downloads(store, engine)

    @asynccontextmanager
    async def lifespan(app):
        await engine.qbit.start()
        store.event(Event(source="plex", type="module.started"))
        tasks = []
        if background:
            tasks.append(asyncio.create_task(worker.run()))
            if os.getenv("MANAGER_API_KEY"):
                tasks.append(asyncio.create_task(deliver_events(store, os.getenv("MANAGER_URL", "http://127.0.0.1:8760"), os.environ["MANAGER_API_KEY"])))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.qbit.close()
            store.event(Event(source="plex", type="module.stopped"))

    app = FastAPI(title="Chamoxis Plex module", version="1.0", lifespan=lifespan,
                  dependencies=[Depends(auth)])
    app.state.store, app.state.worker = store, worker

    def public_results(items):
        # Keep authenticated indexer download URLs inside the Plex module.
        result = []
        for item in items:
            item = dict(item)
            link = item.get("enclosure") or item.get("link")
            if link:
                reference = hashlib.sha256(link.encode()).hexdigest()
                store.set("search_result:" + reference, link)
                item["enclosure"] = item["link"] = "result:" + reference
            result.append(item)
        return result

    @app.get("/health")
    async def health():
        return {"status": "ok", "module": "plex", "version": 1}

    @app.get("/config")
    async def config():
        return {"indexer_ids": engine.INDEXER_IDS, "indexer_labels": engine.INDEXER_LABELS,
                "downloads_disabled": engine.DISABLE_TORRENT_DOWNLOAD,
                "prowlarr_configured": bool(engine.PROWLARR_API_KEY)}

    @app.post("/search")
    async def search(body: Search):
        indexers = engine.INDEXER_IDS if body.indexer == "all" else [body.indexer]
        if any(idx not in engine.INDEXER_IDS for idx in indexers):
            raise HTTPException(422, "Unknown indexer")
        items, errors = [], []
        for idx in indexers:
            try:
                xml = await engine.fetch_rss(engine.build_torznab_search_url(body.query, body.limit, idx))
                items.extend(engine.parse_rss_feed(xml, body.limit, engine.indexer_label(idx)))
            except Exception:
                errors.append({"indexer": idx, "error": "Search failed; inspect module logs"})
        received = len(items)
        items = [i for i in items if not engine.is_av1_title(i.get("title", ""))]
        compatible = len(items)
        if body.rank_preferences:
            items = rank_results(items, body.query, year=body.year, quality=body.quality, language=body.language,
                                 season=body.season, episode=body.episode)
        else:
            items = [i for i in items if engine.quality_matches(i.get("title", ""), body.quality)]
            if body.language:
                items = [i for i in items if language_matches(i.get("title", ""), body.language)]
            items.sort(key=lambda i: engine.parse_size_bytes(i.get("size", "")), reverse=True)
        logger.info("Search results received=%s compatible=%s retained=%s ranking=%s indexer_errors=%s",
                    received, compatible, len(items), body.rank_preferences, len(errors))
        return {"items": public_results(items[:body.limit]), "errors": errors}

    @app.get("/rss")
    async def rss(url: str | None = None, limit: int = Query(default=10, ge=1, le=100)):
        xml = await engine.fetch_rss(url or engine.TORZNAB_RSS_URL)
        return {"items": public_results(engine.parse_rss_feed(xml, limit))}

    @app.get("/torrents")
    async def torrents(limit: int = Query(default=10, ge=1, le=1000)):
        return await engine.qbit.list_torrents(limit)

    @app.get("/torrents/{info_hash}")
    async def torrent(info_hash: str):
        return await engine.qbit.get_torrent_by_hash(info_hash)

    @app.post("/downloads", status_code=202)
    async def add(body: AddTorrent):
        if body.link.startswith("result:") and not store.get("search_result:" + body.link[7:]):
            raise HTTPException(422, "Unknown search result; repeat the search")
        try:
            return store.create_task(body.request_id, body.model_dump())
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/tasks")
    async def tasks():
        return store.tasks()

    @app.get("/downloads/status")
    async def download_status(user_id: int = Query(gt=0)):
        owned = [task for task in store.tasks(newest_created=True) if task["payload"].get("user_id") == user_id]
        snapshots = []
        for task in owned[:5]:
            info_hash = (task.get("result") or {}).get("hash")
            torrent = await engine.qbit.get_torrent_by_hash(info_hash) if info_hash and task["state"] == "downloading" else None
            snapshots.append(snapshot(task, torrent))
        return {"downloads": snapshots}

    @app.get("/tasks/{task_id}")
    async def task(task_id: str):
        found = store.task(task_id)
        if not found:
            raise HTTPException(404, "Task not found")
        return found

    @app.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        found = store.task(task_id)
        if not found:
            raise HTTPException(404, "Task not found")
        if found["state"] not in {"queued", "downloading", "needs_review"}:
            raise HTTPException(409, "Task cannot be cancelled in this state")
        if not store.claim(task_id, found["state"], "cancelled"):
            raise HTTPException(409, "Task changed concurrently")
        return store.task(task_id)

    @app.get("/storage")
    async def storage(query: str = "", kind: str = "movies"):
        if kind not in {"movies", "series"}:
            raise HTTPException(422, "Unknown kind")
        roots = engine.PLEX_MOVIES_PATHS if kind == "movies" else engine.PLEX_SERIES_PATHS
        return {"suggestions": engine.suggest_target_directories(query, kind),
                "roots": [{"path": str(p), "free_bytes": shutil.disk_usage(p).free if p.exists() else None} for p in roots]}

    @app.get("/library")
    async def library(title: str, kind: str = "movies"):
        # Local library hints work without a Plex token, but are not an authoritative inventory.
        matches = engine.suggest_target_directories(title, kind)
        return {"matches": matches, "authoritative": False}

    return app
