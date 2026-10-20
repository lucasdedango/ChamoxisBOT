"""Conservative shortcuts for media requests; other messages keep AI routing."""
import re


def search_request(text):
    text = text.strip().replace('’', "'")
    polite = r"(?:(?:tu\s+)?(?:peux|pourrais|pourrait)\s*(?:-\s*tu)?\s+)?"
    verb = r"(?:cherche(?:r)?|recherche(?:r)?|trouve(?:r)?|ajoute(?:r)?|télécharge(?:r)?|telecharge(?:r)?|dl|mets?|récupère|recupere)"
    match = re.match(r"^" + polite + r"(?:me\s+|nous\s+)?" + verb + r"\b\s*(?:-\s*)?(?:moi\b|nous\b)?\s*[:,]?\s*(.*)$", text, re.I | re.S)
    if not match:
        return None
    request = match[1].strip(' "«»')
    # Do not turn negations or references to an existing download into a new search.
    if re.match(r"^(?:pas|plus|rien|jamais)\b|^(?:ce|cet|le dernier|mon dernier|les derniers)\s+(?:torrent|téléchargement|telechargement)\b", request, re.I):
        return None
    return request
