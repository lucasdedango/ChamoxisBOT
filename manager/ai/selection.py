import os


def default_quality():
    quality = os.getenv("SEARCH_DEFAULT_QUALITY", "1080p").strip().lower()
    return quality if quality in {"2160p", "1080p", "720p", "480p"} else "1080p"
