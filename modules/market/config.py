"""Local settings. Legacy token names are read only from this bot's env file."""
import json
import os
from pathlib import Path
from dotenv import dotenv_values

DEFAULTS = {
    'request_timeout_seconds': 25, 'between_searches_seconds': 4,
    'block_backoff_base_minutes': 15, 'block_backoff_max_minutes': 120,
    'notify_recovery': True, 'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
    'use_curl_cffi': True, 'use_curl_cffi_fallback': True, 'curl_impersonate': 'chrome',
    'leboncoin_external_browser_only': True, 'leboncoin_browser_fallback': True,
    'leboncoin_wait_for_manual_verification': True, 'leboncoin_cdp_host': '127.0.0.1',
    'leboncoin_cdp_port': 9222, 'max_searches_per_user': 10,
    'minimum_interval_minutes': 5, 'manual_check_cooldown_seconds': 60,
}


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError) as error:
        raise ValueError(f'Fichier Market illisible : {path.name}; fichier conservé.') from error


def settings():
    env_file = Path(os.getenv('MARKET_ENV_FILE', 'modules/market/.env'))
    values = dotenv_values(env_file, interpolate=False) if env_file.exists() else {}
    def get(name, default='', legacy=None):
        return os.getenv(name, values.get(name, values.get(legacy, default) if legacy else default)) or default
    def ids(name):
        try:
            return {int(value.strip()) for value in get(name).split(',') if value.strip()}
        except ValueError:
            raise ValueError(f'Identifiants Market invalides : {name}') from None
    def number(name, default='0', legacy=None):
        try:
            return int(get(name, default, legacy))
        except ValueError:
            raise ValueError(f'Réglage numérique Market invalide : {name}') from None
    data = Path(get('MARKET_DATA_DIR', 'data/market')).resolve()
    config = {**DEFAULTS, **read_json(data / 'config.json', {})}
    if config.get('leboncoin_cdp_host') != '127.0.0.1':
        raise ValueError('Le port navigateur Market doit rester sur 127.0.0.1.')
    if not isinstance(config['leboncoin_cdp_port'], int) or not 1 <= config['leboncoin_cdp_port'] <= 65535:
        raise ValueError('Port navigateur Market invalide.')
    for field, low, high in (('request_timeout_seconds', 1, 120), ('between_searches_seconds', 1, 300),
                            ('minimum_interval_minutes', 1, 1440), ('max_searches_per_user', 1, 100),
                            ('manual_check_cooldown_seconds', 30, 3600)):
        try:
            value = float(config[field])
        except (ValueError, TypeError):
            raise ValueError(f'Réglage Market invalide : {field}') from None
        if not low <= value <= high:
            raise ValueError(f'Réglage Market invalide : {field}')
    return {'token': get('MARKET_DISCORD_TOKEN', legacy='DISCORD_TOKEN'),
            'channel': number('MARKET_CHANNEL_ID', legacy='DISCORD_CHANNEL_ID'),
            'guild': number('MARKET_GUILD_ID'), 'admins': ids('MARKET_ADMIN_IDS'),
            'users': ids('MARKET_ALLOWED_USER_IDS'), 'data': data,
            'port': number('MARKET_HEALTH_PORT', '8762'), 'config': config}
