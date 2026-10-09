import asyncio
import json
import secrets
import time
from pathlib import Path
from urllib.parse import quote
import aiohttp
import psutil
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from chamoxis_common.http import APIClient
from chamoxis_common.logging import redact
from shared.schemas import AddTorrent, Preferences
from dashboard.settings import Settings, GROUPS
from dashboard.updates import Updater
from manager.ai.selection import default_quality
from manager.ai.preferences import ground_intent
from manager.ai.catalog import identified_intent, library_request


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Body):
    credential: str = Field(min_length=1, max_length=4000)


class ConfigSave(Body):
    group: str
    changes: dict[str, str]


class SearchBody(Body):
    text: str = Field(min_length=1, max_length=1800)
    user_id: int | None = Field(default=None, gt=0)
    media_id: int | None = Field(default=None, gt=0)
    media_kind: str | None = None


class Confirm(Body):
    proposal: str
    index: int = Field(ge=0, le=99)
    confirmed: bool


class Branch(Body):
    branch: str


class Apply(Body):
    plan: str
    confirmed: bool


def create_app(root=None, client_factory=APIClient, updater=None, launch_token=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    settings = Settings(root)
    updater = updater or Updater(root)
    sessions, proposals, failed_logins = {}, {}, []
    mutation_lock = asyncio.Lock()
    launch = {"token": launch_token, "expires": time.time() + 120}
    app = FastAPI(title="Chamoxis Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.updater = updater

    def clean(value):
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        for group in GROUPS:
            values = settings.values(group)
            for key, kind in GROUPS[group]["fields"].items():
                secret = values.get(key) or ""
                if kind == "secret" and len(secret) >= 4:
                    text = text.replace(secret, "[masqué]")
        return redact(text)

    def client(kind, admin=False, timeout=30):
        m, p = settings.values("manager"), settings.values("plex")
        if kind == "manager":
            return client_factory(m.get("MANAGER_URL") or "http://127.0.0.1:8760",
                m.get("MANAGER_ADMIN_API_KEY" if admin else "MANAGER_API_KEY") or "", timeout=timeout)
        return client_factory(m.get("PLEX_MODULE_URL") or "http://127.0.0.1:8761",
                              m.get("PLEX_MODULE_API_KEY") or p.get("PLEX_MODULE_API_KEY") or "", timeout=timeout)

    async def query(kind, path, method="GET", **kwargs):
        try:
            return await client(kind, timeout=210 if path.startswith("/ai/") else 30).request(method, path, **kwargs)
        except Exception as error:
            raise HTTPException(502, f"{kind} indisponible ou requête refusée (HTTP {getattr(error, 'status', '?')})") from error

    @app.middleware("http")
    async def local_only(request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"detail": "Accès local uniquement"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin", "")
            if origin not in {"http://127.0.0.1:8765", "http://localhost:8765"}:
                return JSONResponse({"detail": "Origine refusée"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: https://image.tmdb.org; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    async def authenticated(request: Request):
        token = request.cookies.get("chamoxis_session", "")
        session = sessions.get(token)
        if not session or session["expires"] <= time.time():
            sessions.pop(token, None)
            raise HTTPException(401, "Connecte-toi au dashboard")
        if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
            raise HTTPException(403, "Session invalide; recharge la page")
        return session

    @app.get("/")
    async def index():
        return FileResponse(root / "dashboard/static/index.html")

    @app.get("/assets/{name}")
    async def assets(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(root / "dashboard/static" / name)

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        now = time.time()
        failed_logins[:] = [t for t in failed_logins if t > now - 60]
        if len(failed_logins) >= 10:
            raise HTTPException(429, "Trop d’essais; attendre une minute")
        admin_key = settings.values("manager").get("MANAGER_ADMIN_API_KEY") or ""
        auto = launch["token"] and launch["expires"] > now and secrets.compare_digest(body.credential, launch["token"])
        if not auto and not (admin_key and secrets.compare_digest(body.credential, admin_key)):
            failed_logins.append(now)
            raise HTTPException(401, "Clé d’administration invalide")
        if auto:
            launch["token"] = None
        for expired in [k for k, v in sessions.items() if v["expires"] <= now]:
            sessions.pop(expired)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sessions[token] = {"csrf": csrf, "expires": now + 8 * 3600}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie("chamoxis_session", token, httponly=True, samesite="strict", max_age=8 * 3600)
        return response

    @app.get("/api/session", dependencies=[Depends(authenticated)])
    async def session(request: Request):
        return {"csrf": sessions[request.cookies["chamoxis_session"]]["csrf"]}

    @app.post("/api/logout", dependencies=[Depends(authenticated)])
    async def logout(request: Request):
        sessions.pop(request.cookies.get("chamoxis_session"), None)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie("chamoxis_session")
        return response

    @app.get("/api/overview", dependencies=[Depends(authenticated)])
    async def overview():
        async def fetch(kind, path, **kwargs):
            try:
                return await query(kind, path, **kwargs)
            except HTTPException as error:
                return {"error": error.detail}
        health, services, plex, tasks, storage, series_storage = await asyncio.gather(
            fetch("manager", "/health"), fetch("manager", "/services"), fetch("plex", "/health"),
            fetch("plex", "/tasks"), fetch("plex", "/storage"), fetch("plex", "/storage", params={"kind": "series"}))
        if not storage.get("error") and not series_storage.get("error"):
            storage["roots"] = list({r["path"]: r for r in storage.get("roots", []) + series_storage.get("roots", [])}.values())
        registry = {}
        try:
            from manager.core.service_registry import Registry
            registry = Registry.load(root / "config/services.json").services
        except (OSError, ValueError):
            pass
        service_rows = []
        if isinstance(services, list):
            for s in services:
                spec = registry.get(s["id"])
                service_rows.append({"id": s["id"], "name": s["name"], "status": s["status"], "attempts": s["attempts"],
                    "can_restart": bool(spec and not spec.external_supervisor and spec.type != "http" and spec.desired == "running")})
        safe_tasks = [public_task(t) for t in tasks] if isinstance(tasks, list) else []
        return {"manager": health, "plex": plex, "services": service_rows, "services_error": services.get("error") if isinstance(services, dict) else None,
                "tasks": safe_tasks, "tasks_error": tasks.get("error") if isinstance(tasks, dict) else None,
                "storage": storage, "cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent}

    def public_task(task):
        payload, result = task["payload"], task.get("result") or {}
        keys = {"name", "progress", "state", "dlspeed", "eta", "reason", "moved_files", "mode", "message"}
        return {"id": task["id"], "title": payload.get("title", ""), "user_id": str(payload["user_id"]),
                "state": task["state"], "updated": task["updated"], "prefs": payload.get("prefs", {}),
                "result": {k: clean(v) if isinstance(v, str) else v for k, v in result.items() if k in keys}}

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(authenticated)])
    async def task_detail(task_id: str):
        task = await query("plex", "/tasks/" + quote(task_id, safe=""))
        info_hash = (task.get("result") or {}).get("hash")
        info = None
        if info_hash and task["state"] == "downloading":
            torrent = await query("plex", "/torrents/" + quote(info_hash, safe=""))
            if torrent:
                info = {k: torrent.get(k) for k in ("state", "progress", "dlspeed", "eta", "num_seeds", "num_leechs", "availability", "save_path")}
        return {"task": public_task(task), "torrent": info}

    @app.post("/api/services/{service_id}/restart", dependencies=[Depends(authenticated)])
    async def restart(service_id: str, body: Apply):
        if not body.confirmed:
            raise HTTPException(400, "Confirmation requise")
        try:
            return await client("manager", admin=True).request("POST", "/services/" + quote(service_id, safe="") + "/restart")
        except Exception as error:
            raise HTTPException(409, "Relance refusée : service non géré ou gestionnaire indisponible") from error

    @app.get("/api/library", dependencies=[Depends(authenticated)])
    async def library(kind: str = "all", genre: str = "", query_text: str = "", refresh: bool = False):
        return await query("plex", "/catalog/library", params={"kind": kind, "genre": genre, "query": query_text, "limit": 100, "refresh": refresh})

    @app.post("/api/search", dependencies=[Depends(authenticated)])
    async def search(body: SearchBody):
        intent = (await query("manager", "/ai/analyze", "POST", json={"text": "Ajoute " + body.text}))["intent"]
        intent = ground_intent(intent, body.text)
        configured = settings.values("manager")
        warning = None
        if configured.get("TMDB_ACCESS_TOKEN"):
            if body.media_id:
                if body.media_kind not in {"movies", "series"}:
                    raise HTTPException(422, "Type d’œuvre invalide")
                media = await query("manager", f"/catalog/media/{body.media_kind}/{body.media_id}")
                intent = identified_intent(intent, media)
            else:
                catalog = await query("manager", "/catalog/resolve", "POST", json={"query": intent["title"], "kind": intent["kind"], "year": intent.get("year")})
                warning = catalog.get("warning")
                if catalog.get("selected"):
                    intent = identified_intent(intent, catalog["selected"])
                elif catalog.get("items"):
                    return {"media_choices": catalog["items"], "items": []}
        if intent.get("clarification"):
            return {"clarification": intent["clarification"], "items": []}
        configured = settings.values("manager")
        quality = intent.get("quality") or configured.get("SEARCH_DEFAULT_QUALITY") or default_quality()
        result = await query("plex", "/search", "POST", json={"query": intent["title"], "query_aliases": intent.get("query_aliases", []), "imdb_id": intent.get("imdb_id"), "tmdb_id": intent.get("tmdb_id"), "media_kind": intent["kind"], "quality": quality,
            "prefer_quality": not bool(intent.get("quality")), "language": intent.get("language"), "year": intent.get("year"), "season": intent["season"],
            "episode": intent["episode"], "min_seeders": intent.get("min_seeders"), "selection_policy": True})
        admins = [v.strip() for v in (configured.get("DISCORD_ADMIN_IDS") or "").split(",") if v.strip().isdigit()]
        user_id = body.user_id or (int(admins[0]) if admins else None)
        items = result["items"][:20]
        proposals.update({k: v for k, v in list(proposals.items()) if v["expires"] > time.time()})
        proposal = secrets.token_urlsafe(24)
        prefs = Preferences(kind=intent["kind"], target_name=intent["title"].replace("/", " - ").replace("\\", " - ")[:140] + (f" ({intent['year']})" if intent.get("year") else ""),
            season=intent["season"], episode=intent["episode"], series_mode="single" if intent["episode"] else "complete")
        proposals[proposal] = {"items": items, "prefs": prefs, "user_id": user_id, "expires": time.time() + 300}
        # Opaque module refs stay on the server, never trust a browser-provided torrent URL.
        public = [{k: i.get(k) for k in ("title", "size", "seeders", "source", "selection_reason", "availability_warning", "identity_warning")} for i in items]
        return {"proposal": proposal, "catalog_warning": warning, "media": intent.get("media"), "intent": {**intent, "quality": quality}, "items": public,
                "quality_options": result.get("quality_options", []), "errors": result.get("errors", []), "user_id": str(user_id) if user_id else None}

    @app.post("/api/search/confirm", dependencies=[Depends(authenticated)])
    async def confirm(body: Confirm):
        found = proposals.get(body.proposal)
        if not body.confirmed or not found or found["expires"] <= time.time() or body.index >= len(found["items"]):
            raise HTTPException(409, "Confirmation invalide ou expirée; refaire la recherche")
        if not found["user_id"]:
            raise HTTPException(400, "Renseigner ton identifiant Discord avant de rechercher")
        item = found["items"][body.index]
        add = AddTorrent(request_id=f"dashboard:{body.proposal}:{body.index}", title=item["title"],
                         link=item.get("enclosure") or item["link"], user_id=found["user_id"], prefs=found["prefs"], confirmed=True)
        result = await query("plex", "/downloads", "POST", json=add.model_dump())
        return {"id": result["id"], "state": result["state"], "warning": item.get("availability_warning")}

    @app.post("/api/chat", dependencies=[Depends(authenticated)])
    async def chat(body: SearchBody):
        if library_request(body.text):
            filters = await query("manager", "/ai/library", "POST", json={"text": body.text})
            result = await query("plex", "/catalog/library", params={**filters, "limit": 8})
            answer = "Disponibles sur Plex :\n" + "\n".join(i["title"] + (f" ({i['year']})" if i.get("year") else "") + " — " + ", ".join(i["genres"]) for i in result["items"])
            return {"result": answer if result["items"] else "Aucune œuvre correspondant aux filtres dans les sections Plex consultées.", "model": "Bibliothèque Plex"}
        return await query("manager", "/ai/chat", "POST", json={"messages": [
            {"role": "system", "content": "Assistant Plex : réponds en français. Tu ne disposes d'aucune fonction d'action dans ce chat. Ne prétends pas avoir consulté ou modifié les services."},
            {"role": "user", "content": body.text}]})

    @app.get("/api/models", dependencies=[Depends(authenticated)])
    async def models():
        config = settings.values("manager")
        async def probe(url, wanted, label):
            if not url:
                return {"label": label, "configured": False}
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                    async with session.get(url.rstrip("/") + "/api/tags", allow_redirects=False) as response:
                        response.raise_for_status()
                        names = [m["name"] for m in (await response.json()).get("models", [])]
                return {"label": label, "configured": True, "available": wanted in names, "model": wanted, "models": names}
            except Exception:
                return {"label": label, "configured": True, "available": False, "model": wanted}
        return await asyncio.gather(probe(config.get("OLLAMA_LOCAL_URL") or "http://127.0.0.1:11434", config.get("OLLAMA_LOCAL_MODEL") or "qwen3:4b", "RTX 2060 / local"),
            probe(config.get("OLLAMA_REMOTE_URL"), config.get("OLLAMA_REMOTE_MODEL") or "qwen3:14b", "Distant"))

    @app.get("/api/config", dependencies=[Depends(authenticated)])
    async def config():
        return settings.display()

    @app.post("/api/config", dependencies=[Depends(authenticated)])
    async def save_config(body: ConfigSave):
        async with mutation_lock:
            try:
                return await asyncio.to_thread(settings.save, body.group, body.changes)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            except OSError as error:
                raise HTTPException(409, "Impossible de sauvegarder la configuration; vérifier les droits et l’espace disque") from error

    @app.get("/api/logs", dependencies=[Depends(authenticated)])
    async def logs(source: str = "manager", level: str = "", task: str = ""):
        def log_path(group, key, default, name):
            directory = Path(settings.values(group).get(key) or default)
            resolved = (root / directory / name).resolve()
            return resolved if resolved.is_relative_to(root) else root / default / name
        paths = {"manager": log_path("manager", "MANAGER_DATA_DIR", "data/manager", "manager.log"),
                 "plex": log_path("plex", "PLEX_DATA_DIR", "data/plex", "plex.log")}
        if not paths["plex"].exists():
            paths["plex"] = root / "bot.log"
        if source not in paths:
            raise HTTPException(400, "Source inconnue")
        path = paths[source]
        if path.is_symlink() or not path.exists():
            return {"text": "Aucun log disponible pour cette source."}
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 200000))
            lines = stream.read(200000).decode("utf-8", "replace").splitlines()[-500:]
        if level:
            lines = [line for line in lines if level.upper() in line.upper()]
        if task:
            lines = [line for line in lines if task in line]
        return {"text": clean("\n".join(lines))}

    @app.get("/api/updates", dependencies=[Depends(authenticated)])
    async def update_status():
        try:
            return await asyncio.to_thread(updater.status)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/updates/branches", dependencies=[Depends(authenticated)])
    async def branches():
        async with mutation_lock:
            try:
                return {"branches": await asyncio.to_thread(updater.branches)}
            except ValueError as error:
                raise HTTPException(400, str(error)) from error

    @app.post("/api/updates/prepare", dependencies=[Depends(authenticated)])
    async def prepare(body: Branch):
        async with mutation_lock:
            try:
                return await asyncio.to_thread(updater.prepare, body.branch)
            except (ValueError, json.JSONDecodeError) as error:
                raise HTTPException(409, str(error)) from error

    @app.post("/api/updates/apply", dependencies=[Depends(authenticated)])
    async def apply_update(body: Apply):
        if not body.confirmed:
            raise HTTPException(400, "Confirmation requise")
        async with mutation_lock:
            # Outages/timeouts/401 do not prove a service stopped. Check local listening
            # sockets as well as API health, and fail closed on uncertain process state.
            try:
                ports = {int(url.rsplit(":", 1)[1].split("/", 1)[0]) for url in (client("manager").url, client("plex").url)}
                listening = {c.laddr.port for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN}
                running_modules = False
                for process in psutil.process_iter(["cmdline"]):
                    args = process.info.get("cmdline") or []
                    if any(args[i] == "-m" and args[i + 1] in {"manager.main", "modules.plex.main"} for i in range(len(args) - 1)):
                        running_modules = True
                        break
            except Exception as error:
                raise HTTPException(409, "Impossible de vérifier l’arrêt des services") from error
            if ports & listening or running_modules:
                raise HTTPException(409, "Arrête d’abord les fenêtres du gestionnaire et du module Plex; laisse le dashboard ouvert")
            try:
                return await asyncio.to_thread(updater.apply, body.plan)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error

    return app
