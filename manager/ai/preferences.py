"""Ground explicit filters in the user's words rather than model guesses."""
import re
import unicodedata


def explicit_preferences(text):
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    def last(pattern):
        matches = list(re.finditer(pattern, value))
        return matches[-1] if matches else None
    year = last(r"\b((?:19|20)\d{2})\b")
    quality = last(r"\b(2160|1080|720|480)p?\b")
    season = last(r"\bs\s*(\d{1,2})(?:\s*e\s*(\d{1,3}))?\b|\bsaison\s*(\d{1,2})\b")
    episode = last(r"\bepisode\s*(\d{1,3})\b")
    language = last(r"\b(francais|french|anglais|english|vff|vfi|vf|multi)\b")
    seeds = re.search(r"\b(?:seeds?|seeders?)\b", value)
    count = re.search(r"(?:au moins|minimum|min\.?|avec)\s+(\d+)\s+(?:seeds?|seeders?)\b", value)
    return {"year": int(year[1]) if year else None,
            "quality": quality[1] + "p" if quality else None,
            "language": ({"francais": "français", "french": "français", "anglais": "anglais", "english": "anglais",
                          "vff": "français", "vfi": "français", "vf": "français", "multi": "multi"}[language[1]]) if language else None,
            "season": int(season[1] or season[3]) if season else 0,
            "episode": int(season[2]) if season and season[2] else int(episode[1]) if episode else 0,
            "min_seeders": max(1, min(int(count[1]), 1000000)) if count else 1 if seeds else None}


def ground_intent(intent, source):
    grounded = {**intent, **explicit_preferences(source)}
    if grounded["season"] or grounded["episode"]:
        grounded["kind"] = "series"
    return grounded
