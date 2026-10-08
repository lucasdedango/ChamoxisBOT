import asyncio
import logging
from chamoxis_common.http import APIClient

logger = logging.getLogger(__name__)


async def deliver_events(store, manager_url, key):
    client = APIClient(manager_url, key, timeout=10)
    while True:
        for event in store.pending_events():
            try:
                await client.request("POST", "/events", json=event)
                store.delivered(event["id"])
            except (OSError, RuntimeError, asyncio.TimeoutError):
                store.retry(event["id"])
                logger.warning("Manager unavailable; retaining event %s", event["id"])
            except Exception:
                store.retry(event["id"])
                logger.exception("Event delivery failed: %s", event["id"])
        await asyncio.sleep(2)
