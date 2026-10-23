import json
import logging
import re
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def redact_tracker_urls(message, replacement="[redacted]"):
    # Cover plain tracker URLs and one/two levels of URL encoding inside magnets.
    slash = r"(?:/|%2f|%252f)"
    message = re.sub(r"(?i)(" + slash + r"announce" + slash + r")(?:(?!%2f|%252f)[^/\s?&#'\"<>\\])+", lambda m: m[1] + replacement, message)
    message = re.sub(r"(?i)(" + slash + r")[a-z0-9_-]{16,}(" + slash + r"announce\b)", lambda m: m[1] + replacement + m[2], message)
    return re.sub(r"(?i)((?:passkey|authkey)(?:\s*[=:]\s*|%3d|%253d))[^&\s,;'\"<>]+", lambda m: m[1] + replacement, message)


def redact(message):
    message = redact_tracker_urls(message)
    for name in ("TMDB_ACCESS_TOKEN", "DISCORD_TOKEN", "QBIT_PASS", "PROWLARR_API_KEY", "PLEX_TOKEN", "MANAGER_API_KEY", "MANAGER_ADMIN_API_KEY", "PLEX_MODULE_API_KEY"):
        value = os.getenv(name, "")
        if len(value) >= 6:
            message = message.replace(value, "[redacted]")
    message = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+", r"\1[redacted]", message)
    return re.sub(r"(?i)((?:apikey|api_key|token|password|passwd|x-api-key)\s*[=:]\s*)[^&\s,;]+", r"\1[redacted]", message)


class JSONFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        message = redact(message)
        return json.dumps({"time": self.formatTime(record), "level": record.levelname,
                           "logger": record.name, "message": message}, ensure_ascii=False)


def configure(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    stream = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    stream.setFormatter(JSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler, stream], force=True)
