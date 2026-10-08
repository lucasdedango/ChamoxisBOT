import asyncio
import aiohttp


class OllamaClient:
    def __init__(self, url, model, timeout=90, context=4096):
        self.url, self.model = url.rstrip("/"), model
        self.timeout, self.context = timeout, context
        self.lock = asyncio.Lock()

    async def available(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(self.url + "/api/tags", allow_redirects=False) as response:
                    if response.status != 200:
                        return False
                    body = await response.json()
                    return self.model in {m.get("name") for m in body.get("models", [])}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    async def chat(self, messages, schema=None):
        # Waiting for the semaphore also consumes the request time budget.
        async with asyncio.timeout(self.timeout):
            async with self.lock:
                payload = {"model": self.model, "messages": messages, "stream": False,
                           "think": False, "keep_alive": "2m",
                           "options": {"num_ctx": self.context, "num_predict": 512, "temperature": 0}}
                if schema is not None:
                    payload["format"] = schema
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                    async with session.post(self.url + "/api/chat", json=payload, allow_redirects=False) as response:
                        if response.status != 200:
                            raise RuntimeError(f"Ollama HTTP {response.status}")
                        body = await response.json()
                        content = body.get("message", {}).get("content")
                        if not isinstance(content, str) or not content.strip():
                            raise ValueError("Ollama returned no content")
                        return content
