"""Independent Market process; never starts the Plex Discord client."""
import argparse
import asyncio
import logging
import os
import re
from pathlib import Path
from aiohttp import web
import discord
from discord import app_commands
from filelock import FileLock, Timeout
from chamoxis_common.logging import configure
from chamoxis_common.store import Store
from .access import Access
from .config import settings, read_json
from . import runtime as rt

logger = logging.getLogger('chamoxisbot.market')


def initialize(config):
    if not config['token'] or not config['channel'] or not config['admins']:
        raise ValueError('Renseigne le token Market, MARKET_CHANNEL_ID et MARKET_ADMIN_IDS dans modules/market/.env.')
    if config['channel'] <= 0 or config['guild'] < 0 or any(value <= 0 for value in config['admins'] | config['users']):
        raise ValueError('Identifiants Discord Market invalides.')
    if not 1 <= config['port'] <= 65535:
        raise ValueError('MARKET_HEALTH_PORT invalide.')
    data = config['data']
    data.mkdir(parents=True, exist_ok=True)
    rt.BASE_DIR = data
    rt.CONFIG_PATH, rt.SEARCHES_PATH, rt.STATE_PATH = (data / name for name in ('config.json', 'searches.json', 'state.json'))
    rt.CONFIG, rt.DISCORD_CHANNEL_ID = config['config'], config['channel']
    searches = read_json(rt.SEARCHES_PATH, [])
    if not isinstance(searches, list):
        raise ValueError('searches.json doit contenir une liste ; fichier conservé.')
    migrated = False
    identifiers = set()
    for search in searches:
        if not isinstance(search, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(search.get('id', ''))):
            raise ValueError('Identifiant de recherche Market invalide ; fichier conservé.')
        if search['id'] in identifiers:
            raise ValueError('Identifiants Market dupliqués ; fichier conservé.')
        identifiers.add(search['id'])
        site, url = rt.validate_search_url(search['url'])
        low, high = rt.validate_interval(search['min_minutes'], search['max_minutes'])
        if low < float(rt.CONFIG['minimum_interval_minutes']):
            raise ValueError(f"Intervalle trop court pour {search['id']} ; ajuste searches.json ou minimum_interval_minutes dans config.json.")
        if 'owner_id' not in search:
            search['owner_id'] = min(config['admins'])
            migrated = True
        if not isinstance(search['owner_id'], int) or search['owner_id'] <= 0:
            raise ValueError('Propriétaire de recherche Market invalide.')
        search.update(site=site, url=url, min_minutes=low, max_minutes=high,
                      name=str(search.get('name') or search['id'])[:80], enabled=bool(search.get('enabled', True)))
    rt.SEARCHES = searches
    if migrated:
        rt.save_json(rt.SEARCHES_PATH, searches)
        logger.info('Recherches Market historiques attribuées au premier administrateur configuré.')
    state = read_json(rt.STATE_PATH, {'searches': {}})
    if not isinstance(state, dict):
        raise ValueError('state.json Market invalide ; fichier conservé.')
    if 'searches' not in state:
        first = next((search for search in searches if search['site'] == 'vinted'), None)
        state = {'searches': {first['id']: state} if first else {}}
        rt.save_json(rt.STATE_PATH, state)
    if not isinstance(state['searches'], dict):
        raise ValueError('state.json Market invalide ; fichier conservé.')
    for status in state['searches'].values():
        if not isinstance(status, dict):
            raise ValueError('État de recherche Market invalide ; fichier conservé.')
        status.pop('last_manual_check', None)  # Monotonic times cannot cross reboots.
    rt.STATE, rt.next_due = state, {}
    rt.search_lock = asyncio.Lock()
    rt.STOP_EVENT.clear()
    rt.LBC_CAPTCHA_OWNER = None
    store = Store(data / 'access.sqlite3')
    rt.ACCESS = Access(store, config['admins'], config['users'])
    return store


class MarketClient(discord.Client):
    def __init__(self, guild_id=0):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree, self.guild_id = app_commands.CommandTree(self), guild_id
        self.scheduler_task = None
        from .commands import register
        register(self.tree)

    async def setup_hook(self):
        await self.tree.sync()
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self):
        rt.MAIN_LOOP = asyncio.get_running_loop()
        logger.info('Bot Market connecté ; permissions et recherches indépendantes du bot Plex.')
        if self.get_channel(rt.DISCORD_CHANNEL_ID) is None:
            logger.warning('Salon Market introuvable dans le cache Discord ; vérifie MARKET_CHANNEL_ID et les permissions du bot.')
        if self.scheduler_task is None or self.scheduler_task.done():
            self.scheduler_task = asyncio.create_task(rt.scheduler(), name='market-scheduler')

    async def close(self):
        rt.STOP_EVENT.set()
        if self.scheduler_task:
            self.scheduler_task.cancel()
            await asyncio.gather(self.scheduler_task, return_exceptions=True)
        await super().close()


def health_app(client):
    app = web.Application()
    async def health(request):
        ready = client.is_ready()
        return web.json_response({'status': 'ok' if ready else 'disconnected', 'module': 'market', 'version': 1}, status=200 if ready else 503)
    app.router.add_get('/health', health)
    return app


async def serve(config):
    rt.client = client = MarketClient(config['guild'])
    @client.tree.error
    async def command_error(interaction, error):
        logger.warning('Commande Market échouée error=%s', type(getattr(error, 'original', error)).__name__)
        text = 'La commande n’a pas pu être terminée. Réessaie ou demande à un administrateur de consulter les logs Market.'
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass
    runner = web.AppRunner(health_app(client), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', config['port']).start()
        await client.start(config['token'])
    finally:
        await client.close()
        rt.MAIN_LOOP = None
        await runner.cleanup()


def import_legacy(source, data):
    source = Path(source).resolve()
    names = ('config.json', 'searches.json', 'state.json')
    if any((data / name).exists() for name in names):
        raise ValueError('Import refusé : des fichiers Market existent déjà dans le dossier de données ; rien n’a été remplacé.')
    files = {name: read_json(source / name, None) for name in names if (source / name).is_file()}
    if not files:
        raise ValueError('Aucun config.json, searches.json ou state.json trouvé dans le dossier source.')
    data.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        rt.save_json(data / name, value)
    print('Fichiers JSON historiques copiés. Le .env et le profil navigateur restent locaux et ne sont pas copiés automatiquement.')


def main():
    parser = argparse.ArgumentParser(description='Bot Market autonome')
    parser.add_argument('--check', action='store_true', help='Valider les fichiers locaux sans connexion Discord ni requête aux sites')
    parser.add_argument('--import-legacy', metavar='DOSSIER', help='Copier les JSON de l’ancien bot dans un dossier de données vide')
    args = parser.parse_args()
    try:
        config = settings()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
    config['data'].mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(config['data'] / 'market.lock'), timeout=0):
            pid_path = config['data'] / 'market.pid'
            pid_path.write_text(str(os.getpid()), encoding='ascii')
            configure(config['data'] / 'market.log')
            if args.import_legacy:
                import_legacy(args.import_legacy, config['data'])
                return
            store = initialize(config)
            try:
                if args.check:
                    print('Configuration Market valide. Aucun accès réseau effectué.')
                else:
                    asyncio.run(serve(config))
            finally:
                rt.STOP_EVENT.set()
                store.close()
    except Timeout:
        try:
            pid = (config['data'] / 'market.pid').read_text(encoding='ascii').strip()
        except OSError:
            pid = ''
        detail = f' PID enregistré : {pid} (à vérifier dans le Gestionnaire des tâches).' if pid.isdecimal() else ''
        raise SystemExit('Une instance Market utilise déjà ce dossier de données.' + detail
                         + ' Ferme uniquement le processus Market concerné ; supprimer market.lock ne libère pas un verrou actif.') from None
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
