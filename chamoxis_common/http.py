import aiohttp


class APIError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Backend HTTP {status}; inspect backend logs for details")


class APIClient:
    def __init__(self, url: str, key: str, timeout: float = 30):
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    async def request(self, method: str, path: str, **kwargs):
        # Never follow redirects carrying the API credential.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
            async with session.request(method, self.url + path, headers={"X-API-Key": self.key},
                                       allow_redirects=False, **kwargs) as response:
                if not 200 <= response.status < 300:
                    raise APIError(response.status)
                return await response.json()
