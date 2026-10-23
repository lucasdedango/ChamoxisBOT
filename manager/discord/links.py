"""Discord links derived from identified catalogue records, never model guesses."""
import re


def tmdb_url(media):
    category = {'series': 'tv', 'movies': 'movie'}.get(media.get('kind'))
    identifier = str(media.get('tmdb_id', ''))
    if not category or not re.fullmatch(r'[1-9][0-9]{0,11}', identifier):
        return None
    return f'https://www.themoviedb.org/{category}/{identifier}'


def media_title(media, limit=110):
    title = str(media.get('title') or 'Sans titre').replace('\n', ' ').replace('\r', ' ')
    title = re.sub(r'([\\*_~`|\[\]])', r'\\\1', title)[:limit].rstrip('\\')
    url = tmdb_url(media)
    return f'[{title}]({url})' if url else title
