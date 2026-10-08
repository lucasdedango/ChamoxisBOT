"""HTTP-only Plex adapter. No imports from the Plex application."""
import os
import re
import urllib.parse
from chamoxis_common.http import APIClient


def plex_client():
    return APIClient(os.getenv("PLEX_MODULE_URL", "http://127.0.0.1:8761"),
                     os.getenv("PLEX_MODULE_API_KEY", ""), timeout=120)


def manager_client():
    return APIClient(os.getenv("MANAGER_URL", "http://127.0.0.1:8760"),
                     os.getenv("MANAGER_API_KEY", ""), timeout=210)


class RemoteQbit:
    async def start(self):
        await plex_client().request("GET", "/health")

    async def close(self):
        pass

    async def list_torrents(self, limit=10):
        return await plex_client().request("GET", "/torrents", params={"limit": limit})

    async def get_torrent_by_hash(self, info_hash):
        return await plex_client().request("GET", "/torrents/" + urllib.parse.quote(info_hash, safe=""))


qbit = RemoteQbit()


async def suggest_target_directories(query, kind):
    return (await plex_client().request("GET", "/storage", params={"query": query, "kind": kind}))["suggestions"]


def parse_torrent_input(link):
    value = link.strip()
    if value.startswith("magnet:?"):
        return {"kind": "magnet", "normalized_value": value}
    if value.startswith(("https://", "http://")) and ".torrent" in urllib.parse.urlparse(value).path.lower():
        return {"kind": "torrent_url", "normalized_value": value}
    raise ValueError("Fournis un magnet ou une URL .torrent")


def parse_size_bytes(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def default_mode_from_query(query):
    match = re.search(r"(?:S|Season\s*|Saison\s*)(\d{1,2})(?:E(\d{1,3}))?", query, re.I)
    if not match:
        match = re.search(r"(\d{1,2})x(\d{1,3})", query, re.I)
    return ("series", int(match[1]), int(match[2] or 0)) if match else ("movies", 0, 0)


def indexer_label(indexer_id):
    return f"Indexer {indexer_id}"
