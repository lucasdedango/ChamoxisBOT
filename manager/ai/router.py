import asyncio
import logging

logger = logging.getLogger(__name__)


class AIUnavailable(RuntimeError):
    pass


class Router:
    def __init__(self, backends):
        self.backends = backends

    async def generate(self, messages, schema=None, validate=None):
        for backend in self.backends:
            try:
                if not await backend.available():
                    continue
                content = await backend.chat(messages, schema)
                result = validate(content) if validate else content
                return {"backend": backend.url, "model": backend.model, "result": result}
            except asyncio.CancelledError:
                raise
            except Exception as error:
                status = getattr(error, 'status', None)
                status = status if isinstance(status, int) else None
                # Exception messages may contain prompts, model output or credentials.
                logger.warning("Ollama backend failed; trying next configured model error=%s status=%s",
                               type(error).__name__, status)
        raise AIUnavailable("No configured model is available or returned a valid response")
