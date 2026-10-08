"""Extracted historical Plex algorithms. No Discord dependency.

Kept together during the first migration to preserve runtime semantics.
"""
from modules.plex.config import *
from modules.plex.core.torrents import QbitApiError, QbitClient
from modules.plex.core.search import _torznab_headers, build_torznab_search_url, download_torrent_with_retry, fetch_rss, is_av1_title, parse_rss_feed, parse_size_bytes, quality_matches, torznab_request
from modules.plex.core.storage import NoStorageAvailableError, estimate_required_bytes, pick_storage_root

class ImportPrefs(TypedDict, total=False):
    kind: str
    target_name: str
    series_mode: str
    season: int
    episode: int


class TrackedTorrent(TypedDict):
    user_id: int
    user_label: str
    prefs: ImportPrefs


def _parse_indexer_ids(raw: str) -> list[str]:
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    if not ids and PROWLARR_INDEXER_ID:
        ids = [PROWLARR_INDEXER_ID]
    return ids


def indexer_label(indexer_id: str) -> str:
    return INDEXER_LABELS.get(indexer_id, f"Indexer {indexer_id}")


def parse_library_paths(raw: str, var_name: str) -> List[Path]:
    if not raw.strip():
        raise RuntimeError(f"{var_name} manquant ou vide dans .env")
    out: List[Path] = []
    for chunk in raw.split("|"):
        txt = chunk.strip().strip('"').strip("'")
        if txt:
            out.append(Path(txt))
    if not out:
        raise RuntimeError(f"{var_name} invalide (aucun chemin exploitable)")
    return out


async def plex_refresh(section_id: str) -> Tuple[bool, str]:
    if not (PLEX_URL and PLEX_TOKEN and section_id):
        return False, "Plex non configuré"
    url = f"{PLEX_URL}/library/sections/{section_id}/refresh"
    headers = {"X-Plex-Token": PLEX_TOKEN}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, headers=headers) as r:
                if r.status in (200, 201):
                    return True, "OK"
                return False, f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)


def normalize_spaces(s: str) -> str:
    s = s.replace(".", " ").replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_brackets(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^\)]*\)", " ", s)
    return normalize_spaces(s)


def remove_junk_tokens(s: str) -> str:
    parts = normalize_spaces(s).split(" ")
    cleaned = []
    for p in parts:
        if p.lower() in JUNK_TOKENS:
            continue
        cleaned.append(p)
    return normalize_spaces(" ".join(cleaned))


def extract_season_episode(text: str) -> Optional[Tuple[int, int]]:
    for pat in SERIES_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group("s")), int(m.group("e"))
    return None


def extract_season_only(text: str) -> Optional[int]:
    for pat in SEASON_ONLY_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group("s"))
    return None


def looks_like_series_name(name: str) -> bool:
    return extract_season_episode(name) is not None


def all_video_files(root: Path) -> List[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_EXTS else []
    out: List[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            out.append(p)
    return out


def pick_biggest_video(files: List[Path]) -> Optional[Path]:
    if not files:
        return None
    return max(files, key=lambda x: x.stat().st_size)


def unique_path(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem = dst.stem
    suf = dst.suffix
    parent = dst.parent
    i = 1
    while True:
        cand = parent / f"{stem}-{i}{suf}"
        if not cand.exists():
            return cand
        i += 1


def unique_rel_path(base_dir: Path, rel: Path) -> Path:
    """
    Retourne un chemin relatif non existant sous base_dir en ajoutant des suffixes si besoin.
    """
    candidate = rel
    stem = rel.stem
    suffix = rel.suffix
    parent = rel.parent
    i = 1
    while (base_dir / candidate).exists():
        candidate = parent / f"{stem}-{i}{suffix}"
        i += 1
    return candidate


def _normalize_relpath_for_match(path_like: str) -> str:
    p = path_like.replace("\\", "/").strip().lower()
    p = re.sub(r"\s+", " ", p)
    return p


def _qbit_destination_path(authoritative_old: str, requested_old: Path, requested_new: Path) -> Path:
    """Keep qBittorrent's root prefix when the filesystem-relative path omitted it."""
    old_parts = [part for part in requested_old.as_posix().split("/") if part not in {"", "."}]
    authoritative_parts = [part for part in authoritative_old.replace("\\", "/").split("/") if part]
    if old_parts and len(authoritative_parts) > len(old_parts):
        tail = authoritative_parts[-len(old_parts):]
        if [part.lower() for part in tail] == [part.lower() for part in old_parts]:
            prefix = authoritative_parts[:-len(old_parts)]
            return Path(*prefix, *requested_new.as_posix().split("/"))
    return requested_new


async def rename_file_resilient(info_hash: str, old_rel: Path, new_rel: Path):
    """
    Renomme avec un état frais et rend une reprise après import partiel idempotente.

    Un HTTP 409 est volontairement conservé comme conflit générique: qBittorrent
    l'utilise pour plusieurs erreurs et pas uniquement pour un oldPath absent.
    """
    torrent = await qbit.get_torrent_by_hash(info_hash)
    files = await qbit.list_files(info_hash)
    names = [str(item.get("name", "")) for item in files]
    wanted = _normalize_relpath_for_match(old_rel.as_posix())
    wanted_name = _normalize_relpath_for_match(old_rel.name)
    destination = _normalize_relpath_for_match(new_rel.as_posix())

    if any(
        _normalize_relpath_for_match(name) == destination
        or _normalize_relpath_for_match(name).endswith("/" + destination)
        for name in names
    ):
        logger.info("Rename already applied: hash=%s old=%s new=%s", info_hash, old_rel, new_rel)
        return

    candidates = []
    for name in names:
        norm = _normalize_relpath_for_match(name)
        if norm == wanted or norm.endswith("/" + wanted) or norm.endswith("/" + wanted_name):
            candidates.append(name)
    picked = old_rel.as_posix()
    if candidates:
        exact = [name for name in candidates if _normalize_relpath_for_match(name) == wanted]
        picked = exact[0] if exact else sorted(candidates, key=len)[-1]

    qbit_new_rel = _qbit_destination_path(picked, old_rel, new_rel)
    try:
        await qbit.rename_file(info_hash, Path(picked), qbit_new_rel)
        return
    except QbitApiError as error:
        if error.status != 409:
            raise
        refreshed_torrent = await qbit.get_torrent_by_hash(info_hash)
        refreshed_files = await qbit.list_files(info_hash)
        logger.error(
            "qBittorrent rename conflict: hash=%s old=%s resolved_old=%s new=%s state=%s "
            "save_path=%s content_path=%s files=%s response=%r",
            info_hash, old_rel, picked, qbit_new_rel,
            (refreshed_torrent or torrent or {}).get("state", "missing"),
            (refreshed_torrent or torrent or {}).get("save_path", ""),
            (refreshed_torrent or torrent or {}).get("content_path", ""),
            [item.get("name", "") for item in refreshed_files], error.response,
        )
        raise RuntimeError(
            f"Conflit qBittorrent pendant le renommage (HTTP 409, hash={info_hash}, "
            f"old={old_rel.as_posix()}, new={new_rel.as_posix()}, réponse={error.response!r})"
        ) from error


async def move_torrent_and_wait(info_hash: str, destination: Path, timeout: float = 300.0) -> dict:
    """Move only after renames, then wait until qBittorrent exposes stable fresh paths."""
    await qbit.set_location(info_hash, destination)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: dict | None = None
    destination_norm = os.path.normcase(os.path.normpath(str(destination)))
    while loop.time() < deadline:
        last = await qbit.get_torrent_by_hash(info_hash)
        if not last:
            raise RuntimeError(f"Torrent disparu pendant le déplacement: {info_hash}")
        state = str(last.get("state", ""))
        save_path = os.path.normcase(os.path.normpath(str(last.get("save_path", ""))))
        content_path = os.path.normcase(os.path.normpath(str(last.get("content_path", ""))))
        moving = "moving" in state.lower()
        if not moving and (save_path == destination_norm or content_path == destination_norm or content_path.startswith(destination_norm + os.sep)):
            await qbit.list_files(info_hash)  # force a final authoritative refresh
            logger.info("Torrent move completed: hash=%s destination=%s state=%s", info_hash, destination, state)
            return last
        await asyncio.sleep(1)
    raise TimeoutError(
        f"Déplacement qBittorrent non confirmé après {timeout:.0f}s: hash={info_hash}, "
        f"destination={destination}, dernier_état={last!r}"
    )


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def sanitize_path_component(name: str, fallback: str = "Unknown") -> str:
    """
    Nettoie un nom de dossier/fichier pour Windows (retire les caractères invalides).
    """
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', " ", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback


def move_file(src: Path, dst: Path, logs: List[str] | None = None) -> Path:
    """
    Ancienne fonction de déplacement local (conservée pour compatibilité).
    Les imports utilisent désormais l'API qBittorrent pour déplacer/renommer.
    """
    ensure_dir(dst.parent)
    dst2 = unique_path(dst)
    shutil.move(str(src), str(dst2))
    if logs is not None:
        logs.append(f"{src} → {dst2}")
    return dst2


def cleanup_empty_dirs(root: Path):
    if not root.exists() or not root.is_dir():
        return
    for p in sorted(root.rglob("*"), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


def pick_content_path(t: dict) -> Optional[Path]:
    cp = t.get("content_path")
    if cp:
        p = Path(cp)
        if p.exists():
            return p
    sp = t.get("save_path")
    name = t.get("name")
    if sp and name:
        p = Path(sp) / name
        if p.exists():
            return p
    return None


def guess_show_title_from_torrent(torrent_name: str) -> str:
    base = strip_brackets(torrent_name)
    # Coupe avant SxxExx si présent
    for pat in SERIES_PATTERNS:
        m = pat.search(base)
        if m:
            base = base[:m.start()]
            break
    base = remove_junk_tokens(base)
    base = base.strip(" -._")
    return base or "Unknown Show"


def build_movie_title_and_year(stem: str) -> Tuple[str, Optional[str]]:
    base = strip_brackets(stem)
    year = None
    m = YEAR_RE.search(base)
    if m:
        year = m.group(1)
        base = base[:m.start()]
    base = remove_junk_tokens(base)
    base = base.strip(" -._")
    title = base or "Unknown Movie"
    return title, year


def parse_info_hash_from_magnet(magnet: str) -> Optional[str]:
    """
    Extrait l'info hash (btih) depuis un lien magnet.
    """
    m = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})", magnet)
    if not m:
        return None
    return m.group(1).lower()


def parse_torrent_input(link: str) -> Dict[str, str]:
    value = (link or "").strip()
    if value.lower().startswith("magnet:?"):
        return {"kind": "magnet", "normalized_value": value}
    if value.lower().startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(value)
        path = parsed.path.lower()
        if path.endswith(".torrent") or ".torrent?" in value.lower():
            return {"kind": "torrent_url", "normalized_value": value}
    raise ValueError("Lien invalide: fournis un magnet ou une URL HTTP(S) vers un fichier .torrent.")


async def add_torrent_from_link(link_kind: str, link_value: str, category: str) -> str:
    if link_kind == "magnet":
        await qbit.add_magnet(link_value, category=category)
        return "magnet"

    # URL .torrent: tentative URL directe puis fallback upload
    try:
        await qbit.add_magnet(link_value, category=category)
        return "url"
    except Exception as e:
        logger.warning("Direct URL add failed (%r). Falling back to torrent upload.", e)
    torrent_bytes = await download_torrent_with_retry(link_value)
    await qbit.add_torrent_file(torrent_bytes, filename="download.torrent", category=category)
    return "upload .torrent"


def find_torrent_by_name_sync(items: List[dict], name: str) -> Optional[dict]:
    for t in items:
        if t.get("name") == name:
            return t
    return None


def _normalize_for_match(text: str) -> str:
    t = normalize_spaces(text).lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def find_recent_torrent_candidate(items: List[dict], *, title: str, category: str) -> Optional[dict]:
    """
    Trouve le torrent le plus probable juste après un ajout Torznab.
    Stratégies:
    1) nom exact
    2) nom normalisé (équivalent / inclusion)
    3) fallback: dernier torrent de même catégorie
    """
    if not items:
        return None

    exact = find_torrent_by_name_sync(items, title)
    if exact:
        return exact

    wanted = _normalize_for_match(title)
    if wanted:
        for t in items:
            n = _normalize_for_match(t.get("name", ""))
            if not n:
                continue
            if n == wanted or wanted in n or n in wanted:
                return t

    same_cat = [t for t in items if (t.get("category") or "").strip().lower() == category.strip().lower()]
    if same_cat:
        same_cat.sort(key=lambda x: int(x.get("added_on", 0)), reverse=True)
        return same_cat[0]

    return None


def hashes_from_torrents(items: List[dict]) -> set[str]:
    return {str(t.get("hash", "")).lower() for t in items if t.get("hash")}


async def resolve_added_torrent(
    *,
    before_hashes: set[str],
    title_hint: str,
    category: str,
    min_added_on: int = 0,
    attempts: int = 6,
    delay: float = 0.7,
) -> Optional[dict]:
    """Retrouve le torrent ajouté après un add via diff de hash, sinon fallback nom/catégorie."""
    latest: List[dict] = []
    for _ in range(attempts):
        latest = await qbit.list_torrents(limit=100)
        if min_added_on > 0:
            latest = [t for t in latest if int(t.get("added_on", 0) or 0) >= min_added_on]
        new_items = [t for t in latest if str(t.get("hash", "")).lower() not in before_hashes]
        if len(new_items) == 1:
            return new_items[0]
        if len(new_items) > 1:
            cand = find_recent_torrent_candidate(new_items, title=title_hint, category=category)
            if cand:
                return cand
        await asyncio.sleep(delay)

    # Dernier fallback: ne considérer que les torrents ajoutés après le début de l'opération.
    filtered = latest
    if min_added_on > 0:
        filtered = [t for t in latest if int(t.get("added_on", 0) or 0) >= min_added_on]
    return find_recent_torrent_candidate(filtered, title=title_hint, category=category)


def shorten(text: str, max_len: int = 90) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def human_size(size_str: str) -> str:
    try:
        n = float(size_str)
    except Exception:
        return "?"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def popularity_badge(item: Dict[str, str]) -> str:
    try:
        seeders = int(item.get("seeders", "") or 0)
    except Exception:
        seeders = 0
    try:
        grabs = int(item.get("grabs", "") or 0)
    except Exception:
        grabs = 0
    # Score simple: seeders comptent plus que les grabs.
    score = seeders * 3 + grabs
    if score >= 200:
        return "🔥"
    if score >= 80:
        return "⭐"
    if score > 0:
        return "👍"
    return "·"


def clamp_embed_field_lines(lines: List[str], limit: int = 1024) -> str:
    """
    Construit un texte <= limit caractères pour un champ embed Discord.
    """
    out: List[str] = []
    used = 0
    for idx, line in enumerate(lines):
        extra = len(line) + (1 if out else 0)
        if used + extra > limit:
            remaining = len(lines) - idx
            suffix = f"… (+{remaining} autres)"
            if out:
                while out and used + 1 + len(suffix) > limit:
                    removed = out.pop()
                    used -= len(removed) + (1 if out else 0)
                if out and used + 1 + len(suffix) <= limit:
                    out.append(suffix)
            elif len(suffix) <= limit:
                out = [suffix]
            break
        out.append(line)
        used += extra
    return "\n".join(out) if out else "Aucun détail"


async def import_torrent_entry(torrent: dict, move_logs: List[str], prefs: ImportPrefs | None = None) -> Tuple[str, bool, bool, int, str]:
    """
    Retourne (message, did_series, did_movies, moved_files, info_hash)
    """
    torrent_name = torrent.get("name", "???")
    category = (torrent.get("category") or "").lower().strip()
    content_root = pick_content_path(torrent)
    if not content_root:
        raise RuntimeError("chemin introuvable")

    info_hash = torrent.get("hash", "")
    prefs = prefs or {}
    is_series = (category == "series") or looks_like_series_name(torrent_name)
    is_forced_movie = (category == "movies")
    if prefs.get("kind") == "movies":
        is_series = False
        is_forced_movie = True
    elif prefs.get("kind") == "series":
        is_series = True
        is_forced_movie = False
    logger.info(
        "Import entry: name=%s hash=%s category=%s is_series=%s forced_movie=%s source=%s",
        torrent_name,
        info_hash,
        category or "-",
        is_series,
        is_forced_movie,
        content_root,
    )

    async def _refresh_content_root_on_missing_video() -> Path:
        refreshed = await qbit.get_torrent_by_hash(info_hash)
        if not refreshed:
            raise RuntimeError("torrent introuvable pendant le rafraîchissement de chemin")
        refreshed_root = pick_content_path(refreshed)
        if not refreshed_root:
            raise RuntimeError("chemin introuvable après rafraîchissement")
        logger.warning("Content path refresh after missing video: old=%s new=%s hash=%s", content_root, refreshed_root, info_hash)
        torrent.update(refreshed)
        return refreshed_root

    if is_series and not is_forced_movie:
        try:
            show, n, target_path = await import_series(torrent, content_root, move_logs, prefs)
        except RuntimeError as e:
            if "aucune vidéo trouvée" not in str(e):
                raise
            logger.warning("Missing video on first series import attempt, refreshing torrent path (hash=%s)", info_hash)
            content_root = await _refresh_content_root_on_missing_video()
            show, n, target_path = await import_series(torrent, content_root, move_logs, prefs)
        msg = f"📺 Série: `{torrent_name}` → {n} fichier(s) dans `{target_path}`"
        return msg, True, False, n, info_hash

    try:
        display, new_path = await import_movie(torrent, content_root, move_logs, prefs)
    except RuntimeError as e:
        if "aucune vidéo trouvée" not in str(e):
            raise
        logger.warning("Missing video on first movie import attempt, refreshing torrent path (hash=%s)", info_hash)
        content_root = await _refresh_content_root_on_missing_video()
        display, new_path = await import_movie(torrent, content_root, move_logs, prefs)
    msg = f"🎬 Film: `{torrent_name}` → `{new_path}`"
    return msg, False, True, 1, info_hash


async def import_series(torrent: dict, content_root: Path, move_logs: List[str] | None = None, prefs: ImportPrefs | None = None) -> Tuple[str, int, Path]:
    r"""
    Déplace TOUS les fichiers vidéo de la série:
    series\Show\Season XX\Show - SXXEYY.ext
    Retourne (show_title, nb_fichiers_deplaces)
    """
    prefs = prefs or {}
    torrent_name = torrent.get("name", "???")
    info_hash = torrent.get("hash", "")
    show_raw = prefs.get("target_name") or guess_show_title_from_torrent(torrent_name)
    show = sanitize_path_component(show_raw, fallback="Series")
    files = all_video_files(content_root)
    if not files:
        raise RuntimeError("aucune vidéo trouvée")
    logger.info("Import series started: torrent=%s hash=%s files=%s source=%s", torrent_name, info_hash, len(files), content_root)

    known: List[Tuple[int, int, Path]] = []
    unknown: List[Path] = []

    for f in files:
        se = extract_season_episode(f.stem)
        if se:
            known.append((se[0], se[1], f))
        else:
            unknown.append(f)

    known.sort(key=lambda x: (x[0], x[1], x[2].name))
    unknown.sort(key=lambda x: x.name)

    required_bytes = estimate_required_bytes(torrent, files)
    base_root = pick_storage_root(PLEX_SERIES_PATHS, required_bytes)
    target_root = base_root / show
    ensure_dir(target_root)

    moved = 0
    season_counts: Dict[int, int] = {}

    # 1) fichiers avec S/E
    series_mode = prefs.get("series_mode", "complete")
    forced_season = int(prefs.get("season", 0) or 0)
    forced_episode = int(prefs.get("episode", 0) or 0)

    for season, ep, f in known:
        season_counts[season] = season_counts.get(season, 0) + 1
        use_season = forced_season if forced_season > 0 else season
        use_episode = forced_episode if (series_mode == "single" and forced_episode > 0) else ep
        season_dir = Path(f"S{use_season:02d}")
        new_filename = f"{show} - S{use_season:02d}E{use_episode:02d}{f.suffix.lower()}"
        desired_rel = season_dir / new_filename
        rel_old = f.relative_to(content_root) if content_root.is_dir() else Path(f.name)
        new_rel = desired_rel if _normalize_relpath_for_match(rel_old.as_posix()) == _normalize_relpath_for_match(desired_rel.as_posix()) else unique_rel_path(target_root, desired_rel)
        # qBittorrent renomme encore dans son emplacement actuel. Le dossier
        # relatif doit donc exister avant le déplacement global.
        if content_root.is_dir():
            ensure_dir(content_root / new_rel.parent)
        await rename_file_resilient(info_hash, rel_old, new_rel)
        moved += 1
        if move_logs is not None:
            move_logs.append(f"{target_root / rel_old} → {target_root / new_rel}")

    # 2) fichiers sans S/E -> on devine la saison
    if season_counts:
        guessed_season = sorted(season_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    else:
        guessed_season = 1

    max_ep = 0
    for s, e, _ in known:
        if s == guessed_season and e > max_ep:
            max_ep = e
    ep_counter = max_ep + 1 if max_ep > 0 else 1

    for f in unknown:
        use_season = forced_season if forced_season > 0 else guessed_season
        use_episode = forced_episode if (series_mode == "single" and forced_episode > 0) else ep_counter
        season_dir = Path(f"S{use_season:02d}")
        new_filename = f"{show} - S{use_season:02d}E{use_episode:02d}{f.suffix.lower()}"
        desired_rel = season_dir / new_filename
        rel_old = f.relative_to(content_root) if content_root.is_dir() else Path(f.name)
        new_rel = desired_rel if _normalize_relpath_for_match(rel_old.as_posix()) == _normalize_relpath_for_match(desired_rel.as_posix()) else unique_rel_path(target_root, desired_rel)
        if content_root.is_dir():
            ensure_dir(content_root / new_rel.parent)
        await rename_file_resilient(info_hash, rel_old, new_rel)
        moved += 1
        ep_counter += 1
        if move_logs is not None:
            move_logs.append(f"{target_root / rel_old} → {target_root / new_rel}")

    await move_torrent_and_wait(info_hash, target_root)
    torrent.update(await qbit.get_torrent_by_hash(info_hash) or {})
    logger.info("Import series completed after verified move: show=%s moved=%s destination=%s", show, moved, target_root)
    return show, moved, target_root


async def import_movie(torrent: dict, content_root: Path, move_logs: List[str] | None = None, prefs: ImportPrefs | None = None) -> Tuple[str, Path]:
    r"""
    Déplace le plus gros fichier vidéo en:
    movies\Title (Year)\Title (Year).ext
    Retourne (display_name, new_path)
    """
    torrent_name = torrent.get("name", "???")
    info_hash = torrent.get("hash", "")
    files = all_video_files(content_root)
    if not files:
        raise RuntimeError("aucune vidéo trouvée")
    logger.info("Import movie started: torrent=%s hash=%s files=%s source=%s", torrent_name, info_hash, len(files), content_root)

    video = pick_biggest_video(files)
    if not video:
        raise RuntimeError("aucune vidéo trouvée")

    prefs = prefs or {}
    if prefs.get("target_name"):
        display = sanitize_path_component(str(prefs["target_name"]), fallback="Movie")
    else:
        title, year = build_movie_title_and_year(video.stem)
        display = sanitize_path_component(f"{title} ({year})" if year else title, fallback="Movie")

    required_bytes = estimate_required_bytes(torrent, files)
    base_root = pick_storage_root(PLEX_MOVIES_PATHS, required_bytes)
    movie_dir = base_root / display
    ensure_dir(movie_dir)

    new_filename = f"{display}{video.suffix.lower()}"
    rel_old = video.relative_to(content_root) if content_root.is_dir() else Path(video.name)
    desired_rel = Path(new_filename)
    new_rel = desired_rel if _normalize_relpath_for_match(rel_old.as_posix()) == _normalize_relpath_for_match(desired_rel.as_posix()) else unique_rel_path(movie_dir, desired_rel)
    await rename_file_resilient(info_hash, rel_old, new_rel)
    await move_torrent_and_wait(info_hash, movie_dir)
    torrent.update(await qbit.get_torrent_by_hash(info_hash) or {})
    new_path = movie_dir / new_rel

    if move_logs is not None:
        move_logs.append(f"{movie_dir / rel_old} → {new_path}")

    logger.info("Import movie completed: display=%s path=%s", display, new_path)
    return display, new_path


def sanitize_series_title(name: str) -> str:
    base = strip_brackets(name)
    for pat in SERIES_PATTERNS:
        m = pat.search(base)
        if m:
            base = base[:m.start()]
            break
    base = remove_junk_tokens(base)
    return normalize_spaces(base).strip(" -._")


def suggest_target_directories(query: str, kind: str, *, max_items: int = 10) -> List[str]:
    needle = sanitize_series_title(query) if kind == "series" else remove_junk_tokens(strip_brackets(query))
    norm_needle = _normalize_for_match(needle)
    scored: List[tuple[int, str]] = []
    roots = PLEX_SERIES_PATHS if kind == "series" else PLEX_MOVIES_PATHS
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for d in root.iterdir():
            if not d.is_dir():
                continue
            n = _normalize_for_match(d.name)
            if not n:
                continue
            score = 0
            if norm_needle and (norm_needle in n or n in norm_needle):
                score = 3
            elif norm_needle:
                tokens = [t for t in re.split(r"\W+", needle.lower()) if t]
                overlap = sum(1 for t in tokens if t and t in d.name.lower())
                if overlap:
                    score = 1 + min(overlap, 2)
            if score > 0:
                scored.append((score, d.name))

    scored.sort(key=lambda x: (-x[0], x[1].lower()))
    names = [name for _, name in scored[:max_items]]
    if not names:
        return []
    dedup: List[str] = []
    for n in names:
        if n not in dedup:
            dedup.append(n)
    return dedup


def extract_quality_options(items: List[Dict[str, str]]) -> List[str]:
    qualities: List[str] = []
    for it in items:
        title = it.get("title", "")
        for q in ("2160p", "1080p", "720p", "480p"):
            if q.lower() in title.lower() and q not in qualities:
                qualities.append(q)
    return qualities


def default_mode_from_query(query: str) -> tuple[str, int, int]:
    se = extract_season_episode(query)
    if se:
        return "series", se[0], se[1]
    season_only = extract_season_only(query)
    if season_only:
        return "series", season_only, 0
    return "movies", 0, 0


qbit = QbitClient(QBIT_URL, QBIT_USER, QBIT_PASS)

PLEX_MOVIES_PATHS = parse_library_paths(PLEX_MOVIES_PATHS_ENV, "PLEX_MOVIES_PATHS") if PLEX_MOVIES_PATHS_ENV else []
PLEX_SERIES_PATHS = parse_library_paths(PLEX_SERIES_PATHS_ENV, "PLEX_SERIES_PATHS") if PLEX_SERIES_PATHS_ENV else []
MIN_FREE_BYTES = int(MIN_FREE_GB * 1024 * 1024 * 1024)
INDEXER_IDS = _parse_indexer_ids(PROWLARR_INDEXER_IDS_ENV)
INDEXER_LABELS = {}
for _pair in PROWLARR_INDEXER_LABELS_ENV.split(','):
    if ':' in _pair:
        _idx, _label = _pair.split(':', 1)
        INDEXER_LABELS[_idx.strip()] = _label.strip()
