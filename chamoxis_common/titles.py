"""Bounded alternate titles from a catalogue's parenthesized dual title."""
import re


def title_variants(title):
    title = title.strip()
    variants = [title] if title else []
    match = re.fullmatch(r'([^()]+?)\s*\(([^()]+)\)', title)
    if not match:
        return variants
    base, alternate = (part.strip() for part in match.groups())
    # Dates, season labels and edition/technical annotations are not movie titles.
    annotation = re.search(r'\b(?:\d+|\d{3,4}p|s\d+e?\d*|version|edition|édition|extended|uncut|cut|remaster\w*|directors?|saison|season|episode|épisode|chapitre|chapter|part|partie|tv|ova|vo|vf|vostfr|multi)\b', alternate, re.I)
    if base and len(alternate) >= 3 and re.search(r'[^\W\d_]', alternate) and not annotation:
        variants.extend([base, alternate])
    return list(dict.fromkeys(variants))
