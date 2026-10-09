import asyncio
import re
import aiohttp


def final_content(body):
    # Some older Qwen/Ollama templates put thinking tags in content even with
    # think=False. Never use message.thinking or publish an unfinished answer.
    content = body.get("message", {}).get("content")
    if body.get("done_reason") == "length":
        raise ValueError("Ollama response exhausted its generation budget")
    if not isinstance(content, str):
        raise ValueError("Ollama returned no content")
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S | re.I).strip()
    if re.search(r"</?think\b", content, re.I) or not content:
        raise ValueError("Ollama returned no complete final answer")
    return content


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
        messages = [dict(message) for message in messages]
        for message in messages:
            if message["role"] == "system":
                message["content"] += "\n/no_think"
        if not any(message["role"] == "system" for message in messages):
            messages.insert(0, {"role": "system", "content": "/no_think"})
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
                        return final_content(body)
