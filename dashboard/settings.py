import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import dotenv_values, set_key

# The browser never chooses a filesystem path or receives credential values.
GROUPS = {
    "manager": {"path": "manager/.env", "fields": {
        "DISCORD_TOKEN": "secret", "DISCORD_GUILD_ID": "ids", "DISCORD_ADMIN_IDS": "ids",
        "DISCORD_ALLOWED_USER_IDS": "ids", "DISCORD_CONVERSATION_CHANNEL_IDS": "ids",
        "DISCORD_NOTIFICATION_CHANNEL_IDS": "ids", "ALERT_CHANNEL_ID": "ids",
        "DISCORD_SERVICE_NOTIFICATIONS": "bool", "SEARCH_DEFAULT_QUALITY": "quality",
        "OLLAMA_LOCAL_URL": "url", "OLLAMA_LOCAL_MODEL": "text", "OLLAMA_REMOTE_URL": "url",
        "OLLAMA_REMOTE_MODEL": "text", "OLLAMA_CONTEXT": "context",
        "MANAGER_API_KEY": "secret", "MANAGER_ADMIN_API_KEY": "secret", "PLEX_MODULE_API_KEY": "secret"}},
    "plex": {"path": "modules/plex/.env", "fields": {
        "QBIT_URL": "url", "QBIT_USER": "text", "QBIT_PASS": "secret", "PROWLARR_URL": "url",
        "PROWLARR_API_KEY": "secret", "PROWLARR_INDEXER_IDS": "ids", "PROWLARR_INDEXER_LABELS": "text",
        "PLEX_URL": "url", "PLEX_TOKEN": "secret", "PLEX_MOVIES_SECTION_ID": "ids",
        "PLEX_SERIES_SECTION_ID": "ids", "PLEX_MOVIES_PATHS": "paths", "PLEX_SERIES_PATHS": "paths",
        "STORAGE_PICK_MODE": "storage", "MIN_FREE_GB": "number", "DISABLE_TORRENT_DOWNLOAD": "bool",
        "TORZNAB_FORCE_UPLOAD": "bool", "CROSS_SEED_ENABLED": "bool",
        "MANAGER_API_KEY": "secret", "PLEX_MODULE_API_KEY": "secret"}}
}


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class Settings:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.backups = self.root / "data/dashboard/config-backups"

    def values(self, group):
        return dict(dotenv_values(self.root / GROUPS[group]["path"]))

    def display(self):
        result = {}
        for group, spec in GROUPS.items():
            values = self.values(group)
            result[group] = [{"key": key, "type": kind, "value": "" if kind == "secret" else values.get(key, "") or "",
                              "configured": bool(values.get(key))} for key, kind in spec["fields"].items()]
        return result

    def validate(self, group, changes):
        if group not in GROUPS or not isinstance(changes, dict) or not changes:
            raise ValueError("Configuration inconnue ou vide")
        for key, value in changes.items():
            kind = GROUPS[group]["fields"].get(key)
            if kind is None or not isinstance(value, str) or len(value) > 4000 or any(c in value for c in "\r\n\x00"):
                raise ValueError("Champ de configuration invalide")
            if kind == "ids" and value and not re.fullmatch(r"\d+(?:,\s*\d+)*", value):
                raise ValueError(f"{key} : identifiants numériques séparés par des virgules")
            if kind == "bool" and value not in {"true", "false"}:
                raise ValueError(f"{key} : true ou false attendu")
            if kind == "quality" and value not in {"2160p", "1080p", "720p", "480p"}:
                raise ValueError("Qualité invalide")
            if kind in {"number", "context"} and (not value.isdigit() or int(value) > 1000000 or kind == "context" and int(value) < 512):
                raise ValueError(f"{key} : nombre invalide")
            if kind == "storage" and value not in {"weighted_random", "random"}:
                raise ValueError("Mode de stockage invalide")
            if kind == "url" and value:
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError(f"{key} : URL HTTP sans identifiants ni paramètres attendue")
            if kind == "paths" and value and any(not Path(p).is_dir() for p in value.split("|")):
                raise ValueError(f"{key} : les dossiers doivent exister sur ce PC")

    def save(self, group, changes):
        self.validate(group, changes)
        path = self.root / GROUPS[group]["path"]
        if path.is_symlink():
            raise ValueError("Les liens symboliques de configuration ne sont pas pris en charge")
        self.backups.mkdir(parents=True, exist_ok=True)
        backup = self.backups / (stamp() + "-" + group + ".env")
        if path.exists():
            shutil.copy2(path, backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".dashboard-env-")
        os.close(fd)
        temporary = Path(name)
        try:
            if path.exists():
                shutil.copy2(path, temporary)
            for key, value in changes.items():
                set_key(temporary, key, value, quote_mode="always")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"saved": True, "backup": str(backup.relative_to(self.root)) if backup.exists() else None,
                "restart": "gestionnaire et dashboard" if group == "manager" else "module Plex"}
