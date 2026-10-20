"""Owner-only ephemeral media search. UI selections never authorize a download."""
import asyncio
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4
import discord
from manager.ai.catalog import identified_intent
from manager.ai.preferences import ground_intent
from manager.ai.selection import default_quality
from manager.core.permissions import allowed_user
from manager.discord.bridge import manager_client, plex_client
from manager.discord.links import media_title
from shared.schemas import AddTorrent, Preferences

logger = logging.getLogger(__name__)
PAGE_SIZE = 20


def escaped(value, limit=150):
    return discord.utils.escape_markdown(str(value))[:limit]


def torrent_menu_details(item):
    try:
        seeds = int(item.get('seeders'))
        if seeds < 0:
            seeds = None
    except (ValueError, TypeError):
        seeds = None
    badge = '🔥' if seeds is not None and seeds >= 20 else '👍' if seeds is not None and seeds >= 3 else '⚠️'
    try:
        size = int(item.get('size'))
    except (ValueError, TypeError):
        size = 0
    size_text = 'taille inconnue'
    if size > 0:
        for unit in ('o', 'Kio', 'Mio', 'Gio', 'Tio'):
            if size < 1024 or unit == 'Tio':
                size_text = f'{size:.2f} {unit}'
                break
            size /= 1024
    return badge, f"Seeds : {seeds if seeds is not None else 'inconnus'} · {size_text}"


def season_date_info(season, brief=False):
    try:
        first_air = date.fromisoformat(season.get('air_date') or '')
    except (ValueError, TypeError):
        return 'Date non annoncée' if brief else 'Date de première diffusion non annoncée dans TMDb.'
    try:
        today = datetime.now(ZoneInfo('Europe/Paris')).date()
    except ZoneInfoNotFoundError:
        # Windows may not have the IANA database; use the server's local calendar.
        today = date.today()
    formatted = first_air.strftime('%d/%m/%Y')
    if first_air > today:
        if brief:
            return f'À venir · {formatted}'
        return f'Première diffusion prévue le {formatted} selon TMDb : saison à venir.'
    if brief:
        return f'Début de diffusion : {formatted}'
    return f'Première diffusion le {formatted} selon TMDb ; cela ne garantit pas que la saison complète soit disponible.'


class PrivateSearch:
    def __init__(self, owner_id, request, manager_factory=manager_client, plex_factory=plex_client, clock=time.monotonic):
        self.owner_id, self.request = owner_id, request
        self.manager_factory, self.plex_factory, self.clock = manager_factory, plex_factory, clock
        self.id, self.generation, self.revision = uuid4().hex, 0, 0
        self.stage, self.page, self.selected = 'new', 0, 0
        self.intent, self.choices, self.items, self.qualities = {}, [], [], []
        self.seasons = []
        self.episode_count = 0
        self.task_id = None
        self.notice, self.warning = '', ''
        self.operation, self.diagnostics = 'analyse de la demande', {}
        self.view, self.message = None, None
        self.lock = asyncio.Lock()
        self.expires = self.clock() + 300

    async def start(self, interaction, acknowledged=False):
        if interaction.user.id != self.owner_id or not allowed_user(self.owner_id):
            await interaction.response.send_message('Recherche non autorisée.', ephemeral=True)
            return
        if not acknowledged:
            await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            if not allowed_user(self.owner_id):
                await interaction.followup.send('Recherche non autorisée : ton accès a été retiré.', ephemeral=True)
                return
            try:
                await self.begin(interaction)
            except Exception as error:
                await self.failed(interaction, error)

    async def begin(self, interaction):
        self.generation += 1
        self.warning, self.page, self.selected = '', 0, 0
        self.stage = 'new'
        self.intent, self.diagnostics, self.items, self.choices, self.qualities = {}, {}, [], [], []
        self.operation = 'analyse de la demande par l’IA'
        if not self.request.strip():
            self.stage, self.notice = 'clarify', 'Précise le film ou la série avec le bouton ci-dessous.'
            await self.show(interaction)
            return
        await self.busy(interaction, 'Identification de l’œuvre…')
        analysis = await self.manager_factory().request('POST', '/ai/analyze', json={'text': 'Ajoute ' + self.request})
        self.intent = ground_intent(analysis['intent'], self.request)
        if self.intent['title'] != 'À préciser':
            self.operation = 'identification de l’œuvre dans TMDb'
            await self.busy(interaction, 'Vérification de l’œuvre dans TMDb…')
            catalog = await self.manager_factory().request('POST', '/catalog/resolve', json={
                'query': self.intent['title'], 'kind': self.intent['kind'], 'year': self.intent.get('year')})
            self.warning = catalog.get('warning') or ''
            if catalog.get('selected'):
                self.intent = identified_intent(self.intent, catalog['selected'])
            elif catalog.get('items'):
                self.choices = catalog['items']
                self.stage, self.page = 'media', 0
                if catalog.get('truncated'):
                    self.warning += ' D’autres œuvres existent : précise le titre si nécessaire.'
                await self.show(interaction)
                return
        if self.intent.get('clarification'):
            self.stage, self.notice = 'clarify', self.intent['clarification']
            await self.show(interaction)
            return
        await self.lookup(interaction)

    async def lookup(self, interaction):
        self.items, self.diagnostics, self.qualities = [], {}, []
        if self.intent['kind'] == 'series' and not self.intent.get('season'):
            self.seasons = [row for row in self.intent.get('media', {}).get('seasons', [])
                            if isinstance(row.get('season'), int) and 1 <= row['season'] <= 100]
            self.seasons = sorted({row['season']: row for row in self.seasons}.values(), key=lambda row: row['season'])
            self.page = 0
            self.stage = 'season' if self.seasons else 'clarify'
            self.notice = 'Quelle saison veux-tu ? TMDb ne fournit pas de liste utilisable pour cette œuvre. Utilise « Corriger la recherche » et indique par exemple S02 ou S02E03.'
            await self.show(interaction)
            return
        intent = self.intent
        prefer = intent.get('prefer_quality', not bool(intent.get('quality')))
        intent['prefer_quality'] = prefer
        intent['quality'] = intent.get('quality') or default_quality()
        self.operation = 'recherche sur les trackers via le module Plex'
        await self.busy(interaction, 'Recherche complète sur les trackers…')
        result = await self.plex_factory().request('POST', '/search', json={
            'query': intent['title'], 'query_aliases': intent.get('query_aliases', []),
            'imdb_id': intent.get('imdb_id'), 'tmdb_id': intent.get('tmdb_id'), 'media_kind': intent['kind'],
            'year': intent.get('year'), 'season': intent.get('season', 0), 'episode': intent.get('episode', 0),
            'quality': intent['quality'], 'prefer_quality': prefer, 'language': intent.get('language'),
            'min_seeders': intent.get('min_seeders'), 'selection_policy': True, 'strict_series': True,
            'rank_preferences': True, 'refined': True, 'limit': 250})
        self.items, self.page, self.selected = result['items'], 0, 0
        self.diagnostics = {key: result.get(key) for key in ('received_count', 'retained_count', 'returned_count', 'indexer_count')}
        self.diagnostics['errors'] = len(result.get('errors') or [])
        if self.items:
            self.stage = 'torrent'
        else:
            self.qualities = result.get('quality_options', [])
            self.stage = 'quality' if self.qualities else 'empty'
            self.notice = 'Aucun torrent compatible avec ces critères.'
            if result.get('received_count') == 0:
                self.notice = 'Les indexers n’ont transmis aucun résultat pour cette recherche.'
            if result.get('errors'):
                self.notice = 'Recherche incomplète : des indexers sont en erreur et aucun torrent compatible n’a été obtenu. Cela ne prouve pas l’absence du contenu.'
            elif result.get('received_count', 0):
                self.notice = 'Des résultats ont été reçus, mais aucun ne correspond aux critères ci-dessous.'
            labels = {'av1': 'codec AV1 exclu', 'title': 'titre différent', 'year': 'année', 'quality': 'qualité', 'language': 'langue', 'season': 'saison', 'episode': 'épisode', 'season_pack': 'saison complète', 'media_kind': 'type film/série', 'seed_preference': 'préférence de seeds', 'seeders': 'minimum de seeds'}
            reasons = result.get('excluded_counts', {})
            if reasons:
                self.notice += '\nExclusions : ' + ', '.join(f'{labels.get(k, k)} : {v}' for k, v in reasons.items())
                self.notice += '\nUn torrent peut cumuler plusieurs motifs d’exclusion.'
        await self.show(interaction)

    async def busy(self, interaction, text):
        self.revision += 1
        if self.view:
            self.view.stop()
        summary = self.understood()
        season_info = self.season_information()
        await interaction.edit_original_response(content=(summary + '\n' if summary else '') + (season_info + '\n' if season_info else '') + text, embed=None, view=None, allowed_mentions=discord.AllowedMentions.none())

    def season_information(self):
        number = self.intent.get('season')
        if self.intent.get('kind') != 'series' or not number:
            return ''
        season = next((s for s in self.intent.get('media', {}).get('seasons', []) if s.get('season') == number), None)
        return f'S{number:02d} — ' + season_date_info(season or {})

    def understood(self):
        title = self.intent.get('title')
        label = media_title(self.intent['media'], 180) if self.intent.get('media') else escaped(title, 180)
        return f'J’ai compris : rechercher **{label}**.' if title and title != 'À préciser' else ''

    def criteria(self):
        intent = self.intent
        parts = ['Type compris : série' if intent.get('kind') == 'series' else 'Type compris : film']
        if intent.get('year'):
            parts.append(str(intent['year']))
        if intent.get('season'):
            scope = f"S{intent['season']:02d}"
            scope += f"E{intent['episode']:02d}" if intent.get('episode') else ' entière'
            parts.append(scope)
        quality = intent.get('quality') or default_quality()
        default = intent.get('prefer_quality', not bool(intent.get('quality')))
        parts.append(quality + (' par défaut ; autres qualités si indisponible' if default else ' demandée'))
        parts.append(intent.get('language') or 'MULTI privilégié')
        parts.append('AV1 seulement en dernier recours')
        if intent.get('min_seeders') is not None:
            parts.append(f"minimum souhaité : {intent['min_seeders']} seeds (repli possible avec avertissement)")
        return ' · '.join(str(part) for part in parts)

    def examples(self):
        title = self.intent.get('title')
        if not title or title == 'À préciser':
            title = 'Les Anneaux de pouvoir'
        title = escaped(title, 100)
        kind = self.intent.get('kind', 'series')
        scope = f" S{self.intent.get('season') or 1:02d}" if kind == 'series' else ''
        if kind == 'series' and self.intent.get('episode'):
            scope += f"E{self.intent['episode']:02d}"
        year = f" {self.intent['year']}" if self.intent.get('year') else ''
        quality = self.intent.get('quality') or default_quality()
        return (f'Formulations conseillées :\n« man cherche {title}{year}{scope} en {quality} »\n'
                '« man ajoute Charlie et la Chocolaterie 2005 en 1080p » (film)\n'
                'Indique le titre exact, l’année si ambiguë, puis la saison ou S01E03 pour un épisode. '
                '« cherche » et « ajoute » ouvrent la même recherche à confirmer. '
                'Utilise **Corriger la recherche** pour modifier ta demande ; un autre libellé ne garantit pas de nouveaux résultats.')

    def embed(self):
        embed = discord.Embed(title='Recherche privée', colour=discord.Colour.blurple())
        if self.stage == 'media':
            embed.title = 'Quelle œuvre veux-tu ?'
            rows = []
            for index in self.visible_indices():
                media = self.choices[index]
                rows.append(f"**{index+1}.** {media_title(media, 80)} ({media.get('year') or 'date inconnue'}) · {'Série' if media['kind']=='series' else 'Film'}")
            embed.description = '\n'.join(rows)
        elif self.stage == 'season':
            embed.title = 'Quelle saison veux-tu ?'
            rows = []
            for index in self.visible_indices():
                season = self.seasons[index]
                count = season.get('episodes')
                rows.append(f"**S{season['season']:02d}** — {escaped(season.get('name') or 'Saison '+str(season['season']), 60)} · {str(count)+' épisodes annoncés' if isinstance(count, int) else 'nombre d’épisodes inconnu'}\n{season_date_info(season, brief=True)}")
            embed.description = '\n'.join(rows) + '\nChoisis la saison avant la recherche de torrents.'
        elif self.stage == 'scope':
            embed.title = f"Saison {self.intent['season']} : entière ou un épisode ?"
            embed.description = 'Choisis **Saison entière** pour un pack de cette saison, ou **Un épisode** pour préciser le numéro. Aucun téléchargement n’est lancé à cette étape.'
        elif self.stage == 'episode':
            embed.title = f"Quel épisode de la saison {self.intent['season']} ?"
            embed.description = '\n'.join(f"**S{self.intent['season']:02d}E{index+1:02d}**" for index in self.visible_indices())
            embed.description += '\nLes numéros proviennent du nombre d’épisodes annoncé par TMDb.'
        elif self.stage in {'torrent', 'uncertain'}:
            item = self.items[self.selected]
            embed.title = 'Torrent sélectionné — confirmation nécessaire'
            embed.description = f"**{self.selected+1}. {escaped(item['title'], 450)}**\n" + escaped(item.get('selection_reason') or '', 250)
            embed.description += '\nSeeds annoncés : ' + escaped(item.get('seeders') if item.get('seeders') not in (None, '') else 'inconnus', 30)
            for name in ('codec_warning', 'availability_warning', 'identity_warning'):
                if item.get(name):
                    embed.description += '\n⚠️ ' + escaped(item[name], 300)
            if self.stage == 'uncertain':
                embed.description += '\n⚠️ Confirmation du module non reçue. « Vérifier l’ajout » reprend exactement la même demande, sans doublon.'
            else:
                embed.description += '\nLe menu change la sélection. Seul « Confirmer l’ajout » lance la demande.'
        else:
            embed.description = escaped(self.notice, 1600)
            if self.stage == 'quality':
                embed.description += '\nChoisis une autre qualité dans le menu ; cela ne confirme aucun téléchargement.'
        media = self.intent.get('media')
        if media and self.stage != 'cancelled':
            embed.add_field(name='Œuvre identifiée · fiche TMDb', value=f"{media_title(media)} ({media.get('year') or 'date inconnue'})", inline=False)
        if self.warning and self.stage not in {'done', 'cancelled'}:
            embed.add_field(name='Information', value=escaped(self.warning, 650), inline=False)
        if self.stage not in {'done', 'cancelled'}:
            if self.season_information():
                embed.add_field(name='Diffusion de la saison', value=self.season_information(), inline=False)
            if self.understood():
                embed.add_field(name='Demande comprise', value=self.understood(), inline=False)
                embed.add_field(name='Critères utilisés', value=escaped(self.criteria(), 650), inline=False)
            if self.diagnostics.get('received_count') is not None:
                embed.add_field(name='Bilan de la recherche', value=f"{self.diagnostics['received_count']} résultats reçus · {self.diagnostics.get('retained_count', '?')} retenus · {len(self.items)} proposés", inline=False)
            if self.diagnostics.get('errors'):
                embed.add_field(name='Recherche incomplète', value=f"{self.diagnostics['errors']} indexer(s) en erreur. D’autres résultats peuvent manquer.", inline=False)
            if self.stage in {'empty', 'quality', 'clarify'}:
                embed.add_field(name='Comment préciser la demande', value=self.examples(), inline=False)
        size = self.page_count_items()
        if self.stage in {'media', 'torrent', 'season', 'episode'}:
            embed.set_footer(text=f'Page {self.page+1}/{max(1, (size+PAGE_SIZE-1)//PAGE_SIZE)} · {PAGE_SIZE} entrées par page · visible uniquement par toi')
            if self.stage == 'torrent':
                embed.set_footer(text=embed.footer.text + ' · Seeds annoncés : 🔥 ≥20 · 👍 3–19 · ⚠️ 0–2 ou inconnus ; connexion non garantie')
        else:
            embed.set_footer(text='Visible uniquement par toi · session valable 5 minutes')
        return embed

    def page_count_items(self):
        if self.stage == 'media':
            return len(self.choices)
        if self.stage == 'season':
            return len(self.seasons)
        if self.stage == 'episode':
            return self.episode_count
        return len(self.items)

    async def choose_episode(self, interaction):
        if self.episode_count <= 0:
            self.stage, self.notice = 'clarify', 'TMDb ne fournit pas les numéros d’épisodes de cette saison. Précise par exemple S02E03 avec le formulaire.'
        else:
            self.stage, self.page = 'episode', 0
        await self.show(interaction)

    def visible_indices(self):
        size = self.page_count_items()
        return range(self.page*PAGE_SIZE, min(size, (self.page+1)*PAGE_SIZE))

    async def show(self, interaction):
        self.revision += 1
        self.expires = self.clock() + 300
        if self.view:
            self.view.stop()
        self.view = SearchView(self)
        self.message = await interaction.edit_original_response(content=None, embed=self.embed(), view=self.view, allowed_mentions=discord.AllowedMentions.none())

    async def failed(self, interaction, error):
        status = getattr(error, 'status', None)
        status = status if isinstance(status, int) else None
        logger.warning('Private search failed owner=%s stage=%s operation=%s error=%s status=%s', self.owner_id, self.stage, self.operation, type(error).__name__, status)
        if self.stage not in {'uncertain', 'done'}:
            reason = 'Le délai de réponse a été dépassé.' if isinstance(error, TimeoutError) else 'Le service n’a pas pu répondre correctement.'
            self.stage, self.notice = 'error', f'Étape en échec : {self.operation}. {reason} Ce n’est pas une recherche sans résultat. Utilise « Réessayer » ; corriger le titre ne résout pas une panne de service.'
        await self.show(interaction)

    async def handle(self, interaction, revision, action, value=None):
        if interaction.user.id != self.owner_id or not allowed_user(self.owner_id):
            await interaction.response.send_message('Cette recherche appartient à un autre utilisateur ou ton accès est désactivé.', ephemeral=True)
            return
        if self.clock() >= self.expires or revision != self.revision or self.stage == 'cancelled':
            await interaction.response.send_message('Cette proposition est expirée ou remplacée. Utilise le dernier menu ou relance la recherche.', ephemeral=True)
            return
        if self.lock.locked():
            await interaction.response.send_message('Une opération est déjà en cours.', ephemeral=True)
            return
        if action == 'refine' and self.stage != 'uncertain':
            await interaction.response.send_modal(RequestModal(self, revision))
            return
        await interaction.response.defer()
        async with self.lock:
            if not allowed_user(self.owner_id):
                await interaction.followup.send('Recherche non autorisée : ton accès a été retiré.', ephemeral=True)
                return
            if revision != self.revision:
                await interaction.followup.send('Cette proposition a été remplacée. Utilise le dernier menu.', ephemeral=True)
                return
            try:
                if action == 'cancel' and self.stage != 'uncertain':
                    self.stage, self.notice = 'cancelled', 'Recherche annulée. Aucun nouvel ajout n’a été demandé.'
                    await self.show(interaction)
                elif action == 'request' and self.stage != 'uncertain':
                    self.request = value.strip()
                    await self.begin(interaction)
                elif action == 'retry' and self.stage == 'error':
                    await self.begin(interaction)
                elif action in {'previous', 'next'} and self.stage in {'media', 'torrent', 'season', 'episode'}:
                    size = self.page_count_items()
                    self.page = max(0, min(self.page + (1 if action == 'next' else -1), (size-1)//PAGE_SIZE))
                    await self.show(interaction)
                elif action == 'season' and self.stage == 'season':
                    number = int(value)
                    season = next((self.seasons[i] for i in self.visible_indices() if self.seasons[i]['season'] == number), None)
                    if season is None:
                        await interaction.followup.send('Choisis une saison de la page affichée.', ephemeral=True)
                        return
                    self.intent['season'] = number
                    count = season.get('episodes')
                    self.episode_count = min(count, 1000) if isinstance(count, int) and count > 0 else 0
                    if self.intent.get('episode'):
                        if self.episode_count and self.intent['episode'] > self.episode_count:
                            await self.choose_episode(interaction)
                        else:
                            await self.lookup(interaction)
                    else:
                        self.stage = 'scope'
                        await self.show(interaction)
                elif action == 'whole_season' and self.stage == 'scope':
                    self.intent['episode'] = 0
                    await self.lookup(interaction)
                elif action == 'single_episode' and self.stage == 'scope':
                    await self.choose_episode(interaction)
                elif action == 'episode' and self.stage == 'episode':
                    episode = int(value)
                    if episode-1 not in self.visible_indices():
                        await interaction.followup.send('Choisis un épisode de la page affichée.', ephemeral=True)
                        return
                    self.intent['episode'] = episode
                    await self.lookup(interaction)
                elif action in {'media', 'torrent'} and self.stage == action:
                    index = int(value)
                    if index not in self.visible_indices():
                        await interaction.followup.send('Sélection invalide pour cette page.', ephemeral=True)
                        return
                    if action == 'media':
                        media = self.choices[index]
                        self.operation = 'lecture de la fiche TMDb choisie'
                        details = await self.manager_factory().request('GET', f"/catalog/media/{media['kind']}/{media['tmdb_id']}")
                        self.intent = identified_intent(self.intent, details)
                        await self.lookup(interaction)
                    else:
                        self.selected = index
                        await self.show(interaction)
                elif action == 'quality' and self.stage == 'quality' and value in self.qualities:
                    self.intent.update(quality=value, prefer_quality=False)
                    await self.lookup(interaction)
                elif action == 'status' and self.stage == 'done':
                    self.operation = 'consultation du suivi du téléchargement'
                    task = await self.plex_factory().request('GET', '/tasks/' + self.task_id)
                    if task.get('payload', {}).get('user_id') != self.owner_id:
                        raise PermissionError('Task ownership mismatch')
                    states = {'queued': 'en attente', 'downloading': 'téléchargement', 'importing': 'import en cours', 'completed': 'terminé', 'needs_review': 'à vérifier', 'simulated': 'mode test', 'cancelled': 'annulé', 'failed': 'échec', 'duplicate': 'doublon'}
                    self.notice = f"Demande {self.task_id} : {states.get(task['state'], task['state'])}. Consulte /telechargements pour les détails après expiration de ce menu."
                    await self.show(interaction)
                elif action == 'confirm' and self.stage in {'torrent', 'uncertain'}:
                    await self.confirm(interaction)
            except Exception as error:
                await self.failed(interaction, error)

    async def confirm(self, interaction):
        item, intent = self.items[self.selected], self.intent
        if intent['kind'] == 'series' and not intent.get('season'):
            await self.lookup(interaction)
            return
        prefs = Preferences(kind=intent['kind'], target_name=intent['title'].replace('/', ' - ').replace('\\', ' - ')[:140] + (f" ({intent['year']})" if intent.get('year') else ''),
            season=intent.get('season', 0), episode=intent.get('episode', 0), series_mode='single' if intent.get('episode') else 'complete')
        body = AddTorrent(request_id=f'private:{self.id}:{self.generation}:{self.selected}',
            link=item.get('enclosure') or item['link'], title=item['title'], user_id=self.owner_id,
            channel_id=None, prefs=prefs, confirmed=True, private=True)
        self.stage = 'uncertain'
        try:
            task = await self.plex_factory().request('POST', '/downloads', json=body.model_dump())
        except Exception:
            self.stage = 'uncertain'
            await self.show(interaction)
            return
        self.task_id = str(task['id'])
        self.stage, self.notice = 'done', f"Demande enregistrée : {task['id']}. Suivi disponible dans /telechargements. Aucun suivi public n’est envoyé pour cette demande. Le mode test du module est respecté."
        await self.show(interaction)


class SearchView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=300)
        self.session, self.revision = session, session.revision
        stage = session.stage
        if stage in {'media', 'torrent'}:
            rows = session.choices if stage == 'media' else session.items
            options = []
            for i in session.visible_indices():
                row = rows[i]
                if stage == 'media':
                    badge = None
                    description = ('Série' if row['kind']=='series' else 'Film') + ' · ' + str(row.get('year') or 'date inconnue')
                else:
                    badge, description = torrent_menu_details(row)
                options.append(discord.SelectOption(label=(str(i+1)+'. '+row['title'])[:100], value=str(i), description=description[:100], emoji=badge, default=stage=='torrent' and i==session.selected))
            self.select = discord.ui.Select(placeholder='Choisir une œuvre' if stage=='media' else 'Choisir un torrent', options=options, row=0)
            async def select(interaction):
                await session.handle(interaction, self.revision, stage, self.select.values[0])
            self.select.callback = select
            self.add_item(self.select)
            size = len(rows)
            self.button('Précédent', 'previous', disabled=session.page==0)
            self.button('Suivant', 'next', disabled=(session.page+1)*PAGE_SIZE>=size)
        elif stage in {'season', 'episode'}:
            options = []
            for index in session.visible_indices():
                if stage == 'season':
                    season = session.seasons[index]
                    label = f"S{season['season']:02d} — {season.get('name') or 'Saison '+str(season['season'])}"
                    description = season_date_info(season, brief=True)
                    value = str(season['season'])
                else:
                    label, description, value = f"S{session.intent['season']:02d}E{index+1:02d}", 'Épisode '+str(index+1), str(index+1)
                options.append(discord.SelectOption(label=label[:100], description=description[:100], value=value))
            self.select = discord.ui.Select(placeholder='Choisir une saison' if stage=='season' else 'Choisir un épisode', options=options, row=0)
            async def choose(interaction):
                await session.handle(interaction, self.revision, stage, self.select.values[0])
            self.select.callback = choose
            self.add_item(self.select)
            self.button('Précédent', 'previous', disabled=session.page==0)
            self.button('Suivant', 'next', disabled=(session.page+1)*PAGE_SIZE>=session.page_count_items())
        elif stage == 'scope':
            self.button('Saison entière', 'whole_season', style=discord.ButtonStyle.primary)
            self.button('Un épisode', 'single_episode')
        elif stage == 'quality':
            select = discord.ui.Select(placeholder='Choisir une qualité', options=[discord.SelectOption(label=q, value=q) for q in session.qualities], row=0)
            async def quality(interaction):
                await session.handle(interaction, self.revision, 'quality', select.values[0])
            select.callback = quality
            self.add_item(select)
        if stage in {'torrent', 'uncertain'}:
            self.button('Vérifier l’ajout' if stage=='uncertain' else 'Confirmer l’ajout', 'confirm', style=discord.ButtonStyle.success)
        if stage == 'done':
            self.button('Actualiser le suivi', 'status')
        if stage == 'error':
            self.button('Réessayer', 'retry')
        if stage not in {'uncertain', 'cancelled'}:
            self.button('Nouvelle recherche' if stage=='done' else 'Corriger la recherche', 'refine')
        if stage not in {'uncertain', 'done', 'cancelled'}:
            self.button('Annuler', 'cancel', style=discord.ButtonStyle.danger)

    def button(self, label, action, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, style=style, disabled=disabled, row=1)
        async def callback(interaction):
            await self.session.handle(interaction, self.revision, action)
        button.callback = callback
        self.add_item(button)

    async def on_timeout(self):
        if self.revision == self.session.revision:
            self.session.expires = 0
            if self.session.message:
                try:
                    await self.session.message.edit(content='Session privée expirée. Relance une recherche.', embed=None, view=None)
                except discord.HTTPException:
                    pass


class RequestModal(discord.ui.Modal, title='Préciser la recherche'):
    request = discord.ui.TextInput(label='Film, série et critères souhaités', style=discord.TextStyle.paragraph, max_length=1800)

    def __init__(self, session, revision):
        super().__init__(timeout=300)
        self.session, self.revision = session, revision
        self.request.default = session.request[:1800]

    async def on_submit(self, interaction):
        await self.session.handle(interaction, self.revision, 'request', self.request.value)


class SearchInvite(discord.ui.View):
    def __init__(self, owner_id, request, manager_factory=manager_client, plex_factory=plex_client):
        super().__init__(timeout=300)
        self.owner_id, self.request = owner_id, request
        self.manager_factory, self.plex_factory = manager_factory, plex_factory
        self.opened = False
        self.message = None

    @discord.ui.button(label='Continuer en privé', style=discord.ButtonStyle.primary)
    async def open(self, interaction, button):
        if interaction.user.id != self.owner_id or not allowed_user(self.owner_id):
            await interaction.response.send_message('Ce bouton est réservé à l’auteur de la recherche.', ephemeral=True)
            return
        if self.opened:
            await interaction.response.send_message('La recherche privée a déjà été ouverte. Utilise son menu ou relance une demande.', ephemeral=True)
            return
        self.opened = True
        button.disabled = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        if getattr(interaction, 'message', None):
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()
        session = PrivateSearch(self.owner_id, self.request, self.manager_factory, self.plex_factory)
        await session.start(interaction, acknowledged=True)

    async def on_timeout(self):
        self.opened = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
