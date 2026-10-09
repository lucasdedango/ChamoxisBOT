import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException
from shared.schemas import Analyze, Chat, Event, Notification, Registration, CatalogResolve
from chamoxis_common.security import require_key
from chamoxis_common.store import Store
from manager.ai.catalog import TMDb, CatalogUnavailable
from manager.ai.gateway import Gateway
from manager.ai.router import AIUnavailable
from manager.core.event_bus import EventBus
from manager.core.notifications import allowed_channel
from manager.core.service_registry import Registry
from manager.core.supervisor import Supervisor


def create_app(store=None, registry=None, gateway=None, background=True):
    key, admin_key = os.getenv("MANAGER_API_KEY", ""), os.getenv("MANAGER_ADMIN_API_KEY", "")
    auth = require_key(key)
    if key == admin_key:
        raise RuntimeError("Administrative and module API keys must be distinct")
    admin_auth = require_key(admin_key)
    store = store or Store(Path(os.getenv("MANAGER_DATA_DIR", "data/manager")) / "state.sqlite3")
    registry = registry or Registry.load(os.getenv("SERVICES_CONFIG", "config/services.json"))
    supervisor, gateway = Supervisor(registry, store=store), gateway or Gateway()
    bus = EventBus(store)
    catalog = TMDb(store)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(supervisor.run()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    # Per-route dependencies allow the administrative credential to be independent.
    app = FastAPI(title="Chamoxis manager", version="1.0", lifespan=lifespan)
    app.state.store, app.state.supervisor = store, supervisor

    @app.get("/health", dependencies=[Depends(auth)])
    async def health():
        return {"status": "ok", "version": 1, "discord": getattr(app.state, "discord_status", "disabled")}

    @app.get("/services", dependencies=[Depends(auth)])
    async def services():
        return supervisor.statuses()

    @app.get("/services/{service_id}/status", dependencies=[Depends(auth)])
    async def status(service_id: str):
        result = next((s for s in supervisor.statuses() if s["id"] == service_id), None)
        if result is None:
            raise HTTPException(404, "Unknown service")
        return result

    @app.post("/services/{service_id}/restart", dependencies=[Depends(admin_auth)])
    async def restart(service_id: str):
        if service_id not in registry.services:
            raise HTTPException(404, "Unknown service")
        try:
            await supervisor.restart(service_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "starting", "id": service_id}

    @app.post("/catalog/resolve", dependencies=[Depends(auth)])
    async def resolve(body: CatalogResolve):
        return await catalog.resolve(body.query, body.kind, body.year)

    @app.get("/catalog/media/{kind}/{media_id}", dependencies=[Depends(auth)])
    async def media(kind: str, media_id: int):
        if kind not in {"movies", "series"} or media_id <= 0:
            raise HTTPException(422, "Invalid media")
        try:
            return await catalog.details(kind, media_id)
        except CatalogUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.post("/ai/library", dependencies=[Depends(auth)])
    async def library_filters(body: Analyze):
        try:
            return await gateway.library(body.text)
        except AIUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.post("/ai/chat", dependencies=[Depends(auth)])
    async def chat(body: Chat):
        try:
            return await gateway.chat(body)
        except AIUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/ai/analyze", dependencies=[Depends(auth)])
    async def analyze(body: Analyze):
        try:
            return await gateway.analyze(body.text)
        except AIUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/ai/route", dependencies=[Depends(auth)])
    async def route(body: Chat):
        try:
            return await gateway.route(body)
        except AIUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/events", dependencies=[Depends(auth)])
    async def event(body: Event):
        if body.channel_id and not allowed_channel(body.channel_id):
            raise HTTPException(403, "Notification channel not allowed")
        return {"accepted": True, "duplicate": not bus.publish(body)}

    @app.post("/notifications", dependencies=[Depends(auth)])
    async def notify(body: Notification):
        if not allowed_channel(body.channel_id):
            raise HTTPException(403, "Notification channel not allowed")
        event = Event(id="notification:" + body.request_id, source="module", type="notification.requested",
                      channel_id=body.channel_id, data={"text": body.text})
        return {"accepted": True, "duplicate": not bus.publish(event)}

    @app.post("/modules/register", dependencies=[Depends(auth)])
    async def register(body: Registration):
        service = registry.services.get(body.id)
        if service is None or not service.health_url or not service.health_url.startswith(body.url.rstrip("/") + "/"):
            raise HTTPException(403, "Module must be predeclared in the service registry")
        return {"registered": body.id, "status": supervisor.states[body.id]["status"]}

    return app
