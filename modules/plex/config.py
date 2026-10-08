import os
import re
import json
import asyncio
import shutil
import logging
import random
from pathlib import Path
from typing import Optional, Tuple, List, Dict, TypedDict
import xml.etree.ElementTree as ET
import urllib.parse
from collections.abc import Mapping

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ----------------- CONFIG -----------------


QBIT_URL = os.getenv("QBIT_URL", "http://127.0.0.1:8080").rstrip("/")
QBIT_USER = os.getenv("QBIT_USER", "")
QBIT_PASS = os.getenv("QBIT_PASS", "")

# Plex (optionnel)
PLEX_URL = os.getenv("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
PLEX_MOVIES_SECTION_ID = os.getenv("PLEX_MOVIES_SECTION_ID", "")
PLEX_SERIES_SECTION_ID = os.getenv("PLEX_SERIES_SECTION_ID", "")

# Dossiers Plex multi-bibliothèques (obligatoire)
PLEX_MOVIES_PATHS_ENV = os.getenv("PLEX_MOVIES_PATHS", "")
PLEX_SERIES_PATHS_ENV = os.getenv("PLEX_SERIES_PATHS", "")
STORAGE_PICK_MODE = os.getenv("STORAGE_PICK_MODE", "weighted_random").strip().lower()
MIN_FREE_GB = float(os.getenv("MIN_FREE_GB", "30"))
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0") or "0")

# Backend Torznab (Prowlarr uniquement)
PROWLARR_URL = os.getenv("PROWLARR_URL", "http://127.0.0.1:9696").rstrip("/")
PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY", "")
PROWLARR_INDEXER_ID = os.getenv("PROWLARR_INDEXER_ID", "1")
PROWLARR_INDEXER_IDS_ENV = os.getenv("PROWLARR_INDEXER_IDS", "").strip()
PROWLARR_INDEXER_LABELS_ENV = os.getenv("PROWLARR_INDEXER_LABELS", "").strip()
TORZNAB_RSS_URL = os.getenv("TORZNAB_RSS_URL", "") or f"{PROWLARR_URL}/api/v1/indexer/{PROWLARR_INDEXER_ID}/newznab/?apikey={PROWLARR_API_KEY}&t=search&limit=20"
TORZNAB_USER_AGENT = os.getenv(
    "TORZNAB_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
)
TORZNAB_COOLDOWN_SECONDS = 30
TORZNAB_FORCE_UPLOAD = os.getenv("TORZNAB_FORCE_UPLOAD", "false").lower() in {"1", "true", "yes", "on"}
DISABLE_TORRENT_DOWNLOAD = os.getenv("DISABLE_TORRENT_DOWNLOAD", "false").lower() in {"1", "true", "yes", "on"}

LOG_FILE = os.getenv("BOT_LOG_FILE", "bot.log")
LOG_LEVEL = os.getenv("BOT_LOG_LEVEL", "INFO").upper()
resolved_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
logger = logging.getLogger("chamoxisbot.plex")

_AUTHORIZATION_RE = re.compile(
    r"(?i)(?P<key>authorization)(?P<sep>\s*[=:]\s*)(?:bearer\s+)?(?P<value>[^&\s,;]+)"
)
_SECRET_RE = re.compile(
    r"(?i)(?P<key>apikey|api_key|token|password|passwd)(?P<sep>\s*[=:]\s*|%3[dD])(?P<value>[^&\s,;]+)"
)


def redact_sensitive(value: object) -> object:
    """Return a log-safe copy while preserving useful URL/request context."""
    if isinstance(value, str):
        value = _AUTHORIZATION_RE.sub(
            lambda m: f"{m.group('key')}{m.group('sep')}***REDACTED***", value
        )
        return _SECRET_RE.sub(lambda m: f"{m.group('key')}{m.group('sep')}***REDACTED***", value)
    if isinstance(value, Mapping):
        return {
            key: ("***REDACTED***" if str(key).lower() in {"apikey", "api_key", "token", "password", "passwd", "authorization"} else redact_sensitive(item))
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.msg)
        record.args = redact_sensitive(record.args)
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(SensitiveDataFilter())
logger.info("Logger initialized (level=%s, file=%s)", LOG_LEVEL, LOG_FILE)

# Ton serveur Discord (sync instant)


# Extensions vidéo
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts"}

# Patterns séries
SERIES_PATTERNS = [
    re.compile(r"\bS(?P<s>\d{1,2})E(?P<e>\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(?P<s>\d{1,2})x(?P<e>\d{1,3})\b", re.IGNORECASE),
]
SEASON_ONLY_PATTERNS = [
    re.compile(r"\bS(?:aison)?\s*(?P<s>\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bSeason\s*(?P<s>\d{1,2})\b", re.IGNORECASE),
]
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

JUNK_TOKENS = {
    "1080p", "720p", "2160p", "4k", "uhd", "hdr", "hdr10", "dv", "dolby", "vision",
    "web", "webrip", "web-dl", "bluray", "bdrip", "brrip", "hdtv", "dvdrip", "remux",
    "x264", "x265", "h264", "h265", "hevc", "av1",
    "aac", "ac3", "dts", "truehd", "atmos",
    "multi", "french", "vostfr", "vost", "vf", "vff", "vfi",
    "proper", "repack", "extended", "uncut",
}
