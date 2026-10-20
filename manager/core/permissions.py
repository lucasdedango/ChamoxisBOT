import os

_store = None


def configure(store):
    """Use the manager's existing SQLite connection, never edit environment files."""
    global _store
    _store = store


def access_settings():
    return _store.get('discord:access', {}) if _store is not None else {}


def access_snapshot():
    settings = access_settings()
    return {'mode': settings.get('mode', 'restricted' if ids('DISCORD_ALLOWED_USER_IDS') else 'open'),
            'configured': sorted(ids('DISCORD_ALLOWED_USER_IDS')),
            'added': sorted(settings.get('added', [])), 'blocked': sorted(settings.get('blocked', [])),
            'admins': sorted(ids('DISCORD_ADMIN_IDS'))}


def update_access(actor_id, user_id=None, *, allow=None, mode=None):
    if not administrator(actor_id):
        raise PermissionError('Configured administrator required')
    if _store is None:
        raise RuntimeError('Access storage unavailable')
    settings = access_settings()
    if mode is not None:
        if mode not in {'open', 'restricted'}:
            raise ValueError('Invalid access mode')
        settings['mode'] = mode
    else:
        if not isinstance(user_id, int) or user_id <= 0 or not isinstance(allow, bool):
            raise ValueError('Invalid user')
        if administrator(user_id):
            raise ValueError('Administrators retain their configured access')
        added, blocked = set(settings.get('added', [])), set(settings.get('blocked', []))
        (added if allow else blocked).add(user_id)
        (blocked if allow else added).discard(user_id)
        settings.update(added=sorted(added), blocked=sorted(blocked))
    _store.set('discord:access', settings)
    return access_snapshot()


def ids(name):
    return {int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip()}


def allowed_user(user_id):
    if administrator(user_id):
        return True
    settings = access_snapshot()
    if user_id in settings['blocked']:
        return False
    return settings['mode'] == 'open' or user_id in settings['configured'] or user_id in settings['added']


def administrator(user_id):
    return user_id in ids("DISCORD_ADMIN_IDS")
