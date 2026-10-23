"""Historical torrents operations extracted without changing their behavior."""
from modules.plex.config import *

class QbitApiError(RuntimeError):
    def __init__(self, status: int, endpoint: str, response: str):
        self.status = status
        self.endpoint = endpoint
        self.response = response
        super().__init__(f"qBittorrent HTTP {status} on {endpoint}: {response or '<empty response>'}")


class QbitClient:
    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url
        self.user = user
        self.password = password
        self.session: aiohttp.ClientSession | None = None
        self.logged_in = False

    async def start(self):
        # CookieJar(unsafe=True) indispensable sur 127.0.0.1 pour garder le SID -> sinon 403
        if self.session is None or self.session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(cookie_jar=jar, timeout=timeout)
        logger.info("QbitClient session started for %s", self.base_url)
        self.logged_in = False

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("QbitClient session closed")

    async def login(self):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        url = f"{self.base_url}/api/v2/auth/login"
        data = {"username": self.user, "password": self.password}
        async with self.session.post(url, data=data) as r:
            text = await r.text()
            if r.status != 200 or text.strip() != "Ok.":
                raise RuntimeError(f"Login qBittorrent échoué: HTTP {r.status} / {text}")
        self.logged_in = True
        logger.info("qBittorrent login successful")

    async def ensure_login(self):
        if not self.logged_in:
            await self.login()

    async def _get_json(self, path: str, params: dict | None = None):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        await self.ensure_login()

        url = f"{self.base_url}{path}"
        logger.debug("qBittorrent GET %s params=%s", path, params)
        async with self.session.get(url, params=params) as r:
            if r.status == 403:
                self.logged_in = False
                await self.ensure_login()
                async with self.session.get(url, params=params) as r2:
                    if r2.status != 200:
                        raise QbitApiError(r2.status, path, await r2.text())
                    return await r2.json()
            if r.status != 200:
                raise QbitApiError(r.status, path, await r.text())
            return await r.json()

    async def _post_text(self, path: str, data: dict):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        await self.ensure_login()

        url = f"{self.base_url}{path}"
        logger.debug("qBittorrent POST %s data_keys=%s", path, list(data.keys()))
        async with self.session.post(url, data=data) as r:
            if r.status == 403:
                self.logged_in = False
                await self.ensure_login()
                async with self.session.post(url, data=data) as r2:
                    if r2.status != 200:
                        raise QbitApiError(r2.status, path, await r2.text())
                    return await r2.text()
            if r.status != 200:
                raise QbitApiError(r.status, path, await r.text())
            return await r.text()

    async def list_torrents(self, limit: int = 10):
        items = await self._get_json("/api/v2/torrents/info", params={"sort": "added_on", "reverse": "true"})
        return items[:limit]

    async def list_completed(self):
        items = await self._get_json("/api/v2/torrents/info", params={"sort": "added_on", "reverse": "true"})
        out = []
        for t in items:
            if float(t.get("progress", 0.0)) >= 1.0:
                out.append(t)
        return out

    async def add_magnet(self, magnet: str, category: str | None = None):
        data = {"urls": magnet}
        if category:
            data["category"] = category
        logger.info("qBittorrent add URL (category=%s)", category or "-")
        await self._post_text("/api/v2/torrents/add", data=data)

    async def add_torrent_file(self, torrent_bytes: bytes, filename: str = "download.torrent", category: str | None = None):
        if not self.session:
            raise RuntimeError("QbitClient.start() n'a pas été appelé.")
        await self.ensure_login()

        async def _post_once():
            form = aiohttp.FormData()
            form.add_field(
                "torrents",
                torrent_bytes,
                filename=filename,
                content_type="application/x-bittorrent",
            )
            if category:
                form.add_field("category", category)
            url = f"{self.base_url}/api/v2/torrents/add"
            logger.info("qBittorrent upload torrent file (%s bytes, category=%s)", len(torrent_bytes), category or "-")
            async with self.session.post(url, data=form) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} / {await r.text()}")

        try:
            await _post_once()
        except RuntimeError as e:
            # Tentative de relogin si le SID a expiré
            if "HTTP 403" in str(e):
                self.logged_in = False
                await self.ensure_login()
                await _post_once()
            else:
                raise

    async def get_torrent_by_hash(self, info_hash: str) -> Optional[dict]:
        items = await self._get_json("/api/v2/torrents/info", params={"hashes": info_hash})
        if not items:
            return None
        return items[0]

    async def set_location(self, info_hash: str, location: Path):
        data = {"hashes": info_hash, "location": str(location)}
        await self._post_text("/api/v2/torrents/setLocation", data=data)

    async def rename_file(self, info_hash: str, old: Path, new: Path):
        data = {"hash": info_hash, "oldPath": old.as_posix(), "newPath": new.as_posix()}
        await self._post_text("/api/v2/torrents/renameFile", data=data)

    async def list_files(self, info_hash: str) -> List[dict]:
        return await self._get_json("/api/v2/torrents/files", params={"hash": info_hash})
