from __future__ import annotations

import asyncio
import html
import json
import os
import random
import math
import re
import time
import threading
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from chamoxis_common.logging import redact
from modules.market.config import read_json
import logging

logger = logging.getLogger("chamoxisbot.market")

def print(*values, **kwargs):
    logger.info(redact(" ".join(str(value) for value in values)))


import discord
import requests
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None
from discord import app_commands
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = Exception

# Filled by initialize(); importing this module does not load tokens or start the bot.
BASE_DIR = Path("data/market")
CONFIG_PATH, SEARCHES_PATH, STATE_PATH = (BASE_DIR / name for name in ("config.json", "searches.json", "state.json"))
ACCESS = None
STOP_EVENT = threading.Event()

VINTED_ORIGIN = "https://www.vinted.fr"
LEBONCOIN_ORIGIN = "https://www.leboncoin.fr"
LBC_BROWSER_PROFILE = BASE_DIR / "browser_profile_lbc"
LBC_BROWSER_LOCK = threading.Lock()
LBC_CYCLE_LOCK = threading.Lock()
LBC_CAPTCHA_OWNER: str | None = None
LBC_VERIFICATION_LOCK = threading.Lock()
LBC_VERIFICATION_ACTIVE: set[str] = set()
MAIN_LOOP: asyncio.AbstractEventLoop | None = None

SUPPORTED_HOSTS = {
    "vinted": ("vinted.fr", "www.vinted.fr"),
    "leboncoin": ("leboncoin.fr", "www.leboncoin.fr"),
}


@dataclass(frozen=True)
class Listing:
    site: str
    item_id: str
    title: str
    price: str
    currency: str
    url: str
    image_url: str | None = None
    location: str | None = None

    @property
    def display_price(self) -> str:
        symbol = "€" if self.currency.upper() == "EUR" else self.currency
        raw = str(self.price).strip().replace(",", ".")
        try:
            value = float(raw)
            if value.is_integer():
                return f"{int(value)} {symbol}"
            return f"{value:.2f} {symbol}".replace(".", ",")
        except ValueError:
            return f"{self.price} {symbol}".strip()


class FetchBlocked(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HumanVerificationPending(RuntimeError):
    """Leboncoin attend une vérification manuelle dans un navigateur déjà ouvert."""
    pass


def load_json(path: Path, default):
    return read_json(path, default)


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def detect_site(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    host = host.lower()
    for site, hosts in SUPPORTED_HOSTS.items():
        if host in hosts:
            return site
    return None


def validate_search_url(url):
    url = url.strip()
    site = detect_site(url)
    path = urlparse(url).path.rstrip('/')
    if len(url) > 2000 or not site or path != ('/catalog' if site == 'vinted' else '/recherche'):
        raise ValueError('Utilise une URL HTTPS de recherche /catalog sur vinted.fr ou /recherche sur leboncoin.fr.')
    return site, url


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return html.unescape(str(value)).strip()


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def normalize_image(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if isinstance(value, dict):
        for key in ("thumb_url", "small_url", "url", "src"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
    if isinstance(value, list):
        for item in value:
            candidate = normalize_image(item)
            if candidate:
                return candidate
    return None


def looks_blocked(text: str) -> bool:
    """Détecte une vraie page de challenge.

    Important : Vinted/Leboncoin chargent parfois leurs scripts anti-bot sur une
    page normale. Cette fonction n'est donc consultée qu'APRÈS tentative de
    parsing des annonces.
    """
    low = text.lower()
    strong_markers = (
        "verify you are human",
        "vérifiez que vous êtes humain",
        "are you a robot",
        "robot or human",
        "access denied",
        "temporarily blocked",
        "too many requests",
        "cf-chl-",
        "challenge-platform",
        "captcha-delivery.com/captcha/",
        "geo.captcha-delivery.com/captcha/",
    )
    return any(marker in low for marker in strong_markers)


def normalize_vinted_source(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r'\\+"', '"', text)
    text = re.sub(r'\\+/', '/', text)
    text = re.sub(r'\\+:', ':', text)
    return text


def parse_vinted(source: str) -> list[Listing]:
    text = normalize_vinted_source(source)
    pattern = re.compile(
        r'"id":(?P<outer_id>\d+),"productItem":\{'
        r'"id":(?P<id>\d+),'
        r'"title":"(?P<title>.*?)",'
        r'"url":"(?P<url>.*?)".*?'
        r'"price":\{"amount":"(?P<price>[0-9.]+)","currencyCode":"(?P<currency>[A-Z]+)"\}.*?'
        r'"thumbnailUrl":"(?P<image>.*?)"',
        re.DOTALL,
    )
    items: list[Listing] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        item_id = match.group("id")
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(Listing(
            site="vinted",
            item_id=item_id,
            title=clean_text(match.group("title").replace(r"\u0026", "&")),
            price=match.group("price"),
            currency=match.group("currency"),
            url=urljoin(VINTED_ORIGIN, match.group("url")),
            image_url=match.group("image") or None,
        ))

    if items:
        return items

    link_pattern = re.compile(
        r'<a[^>]+href="(?P<url>/items/(?P<id>\d+)-[^"?]+)(?:\?[^\"]*)?"[^>]*title="(?P<title>[^"]+)"',
        re.IGNORECASE,
    )
    price_pattern = re.compile(r'(?P<price>\d+(?:[.,]\d{1,2})?)\s*€')
    for match in link_pattern.finditer(text):
        item_id = match.group("id")
        if item_id in seen:
            continue
        title_attr = clean_text(match.group("title"))
        price_match = price_pattern.search(title_attr)
        if not price_match:
            continue
        seen.add(item_id)
        items.append(Listing(
            site="vinted",
            item_id=item_id,
            title=title_attr.split(", Marque:", 1)[0].strip(),
            price=price_match.group("price").replace(",", "."),
            currency="EUR",
            url=urljoin(VINTED_ORIGIN, match.group("url")),
        ))
    return items


def walk_json(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_json_scripts(source: str) -> list[Any]:
    blobs: list[Any] = []
    patterns = [
        re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S),
        re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S),
    ]
    for pattern in patterns:
        for match in pattern.finditer(source):
            raw = html.unescape(match.group(1)).strip()
            try:
                blobs.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return blobs


def leboncoin_from_object(obj: dict) -> Listing | None:
    item_id = first_nonempty(obj.get("list_id"), obj.get("ad_id"), obj.get("id"))
    title = first_nonempty(obj.get("subject"), obj.get("title"), obj.get("name"))
    if item_id is None or not title:
        return None

    # Evite de prendre des objets JSON arbitraires qui ont simplement id + title.
    has_ad_shape = any(k in obj for k in ("list_id", "ad_id", "price", "images", "url", "category_id"))
    if not has_ad_shape:
        return None

    price_obj = obj.get("price")
    currency = "EUR"
    price: Any = None
    if isinstance(price_obj, list) and price_obj:
        price = price_obj[0]
    elif isinstance(price_obj, dict):
        price = first_nonempty(price_obj.get("amount"), price_obj.get("value"), price_obj.get("price"))
        currency = first_nonempty(price_obj.get("currency"), price_obj.get("currencyCode"), "EUR")
    elif price_obj is not None:
        price = price_obj
    price = first_nonempty(price, obj.get("price_cents"))
    if price is None:
        return None
    if "price_cents" in obj and obj.get("price") is None:
        try:
            price = float(price) / 100
        except (TypeError, ValueError):
            pass

    url = first_nonempty(obj.get("url"), obj.get("link"), obj.get("canonical_url"))
    if isinstance(url, dict):
        url = first_nonempty(url.get("url"), url.get("href"))
    if not isinstance(url, str) or not url:
        # URLs Leboncoin courantes. Le type exact de catégorie n'est pas indispensable pour le lien /ad.
        url = f"{LEBONCOIN_ORIGIN}/ad/{item_id}"
    elif url.startswith("/"):
        url = urljoin(LEBONCOIN_ORIGIN, url)

    images = first_nonempty(obj.get("images"), obj.get("image"), obj.get("photos"))
    image_url = normalize_image(images)

    location_obj = obj.get("location")
    location = None
    if isinstance(location_obj, dict):
        location = first_nonempty(
            location_obj.get("city"),
            location_obj.get("label"),
            location_obj.get("zipcode"),
        )
    elif isinstance(location_obj, str):
        location = location_obj

    return Listing(
        site="leboncoin",
        item_id=str(item_id),
        title=clean_text(title),
        price=str(price),
        currency=str(currency),
        url=str(url),
        image_url=image_url,
        location=clean_text(location) or None,
    )


def parse_leboncoin(source: str) -> list[Listing]:
    """Parse en priorité props.pageProps.searchData.ads de __NEXT_DATA__.

    C'est la structure observée dans le code source Leboncoin fourni le
    24/09/2026. On garde ensuite un fallback générique si leur structure change.
    """
    items: list[Listing] = []
    seen: set[str] = set()

    blobs = extract_json_scripts(source)

    # Chemin actuel : __NEXT_DATA__.props.pageProps.searchData.ads
    for blob in blobs:
        try:
            ads = blob["props"]["pageProps"]["searchData"]["ads"]
        except (KeyError, TypeError):
            ads = None
        if isinstance(ads, list):
            for obj in ads:
                if not isinstance(obj, dict):
                    continue
                listing = leboncoin_from_object(obj)
                if listing and listing.item_id not in seen:
                    seen.add(listing.item_id)
                    items.append(listing)
            if items:
                return items

    # Fallback JSON générique.
    for blob in blobs:
        for obj in walk_json(blob):
            listing = leboncoin_from_object(obj)
            if listing and listing.item_id not in seen:
                seen.add(listing.item_id)
                items.append(listing)

    if items:
        return items

    # Dernier fallback pour les payloads JSON échappés/inlinés.
    text = html.unescape(source).replace(r'\"', '"').replace(r'\/', '/')
    patterns = [
        re.compile(
            r'"list_id"\s*:\s*(?P<id>\d+).*?"subject"\s*:\s*"(?P<title>.*?)".*?'
            r'"price"\s*:\s*\[(?P<price>\d+(?:\.\d+)?)\].*?'
            r'"url"\s*:\s*"(?P<url>https?:[^\"]+|/[^\"]+)"',
            re.S,
        ),
        re.compile(
            r'"list_id"\s*:\s*(?P<id>\d+).*?"subject"\s*:\s*"(?P<title>.*?)".*?'
            r'"price"\s*:\s*\[(?P<price>\d+(?:\.\d+)?)\]',
            re.S,
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            item_id = match.group("id")
            if item_id in seen:
                continue
            seen.add(item_id)
            url = match.groupdict().get("url") or f"{LEBONCOIN_ORIGIN}/ad/{item_id}"
            items.append(Listing(
                site="leboncoin",
                item_id=item_id,
                title=clean_text(match.group("title")),
                price=match.group("price"),
                currency="EUR",
                url=urljoin(LEBONCOIN_ORIGIN, url),
            ))
    return items


def _request_with_requests(url: str, headers: dict, timeout: int):
    return _safe_get(requests, url, headers=headers, timeout=timeout)


def _safe_get(transport, url, **kwargs):
    for _ in range(4):
        if not detect_site(url):
            raise ValueError('Redirection Market vers un domaine non autorisé.')
        response = transport.get(url, allow_redirects=False, **kwargs)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        target = urljoin(url, response.headers.get('Location', ''))
        response.close()
        url = target
    raise ValueError('Trop de redirections Market.')


def _request_with_curl(url: str, headers: dict, timeout: int, impersonate: str):
    if curl_requests is None:
        raise RuntimeError("curl_cffi n'est pas installé")
    # curl_cffi reproduit aussi la couche TLS/HTTP d'un navigateur moderne,
    # contrairement à requests qui ne change que les en-têtes HTTP.
    return _safe_get(
        curl_requests, url,
        headers=headers,
        timeout=timeout,
        impersonate=impersonate,
    )





def _resolve_external_browser(config: dict) -> tuple[Path, str]:
    """Trouve le navigateur Chromium à lancer comme processus Windows normal.

    V2.7 : Playwright ne lance plus le navigateur. Il se contente ensuite de
    s'attacher au port DevTools local de l'instance déjà ouverte.
    """
    configured = str(config.get("leboncoin_browser_executable_path", "")).strip()
    candidates: list[tuple[str, Path]] = []
    if configured:
        candidates.append(("Brave/configuré", Path(configured)))

    local = Path(os.environ.get("LOCALAPPDATA", ""))
    pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    pfx86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    candidates.extend([
        ("Brave", local / r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("Brave", pf / r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("Brave", pfx86 / r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("Chrome", local / r"Google\Chrome\Application\chrome.exe"),
        ("Chrome", pf / r"Google\Chrome\Application\chrome.exe"),
        ("Chrome", pfx86 / r"Google\Chrome\Application\chrome.exe"),
        ("Edge", pf / r"Microsoft\Edge\Application\msedge.exe"),
        ("Edge", pfx86 / r"Microsoft\Edge\Application\msedge.exe"),
    ])
    seen: set[str] = set()
    for label, path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path, label
    raise RuntimeError("Aucune installation Brave/Chrome/Edge trouvée")


def _lbc_cdp_url(config: dict) -> str:
    host = str(config.get("leboncoin_cdp_host", "127.0.0.1"))
    port = int(config.get("leboncoin_cdp_port", 9222))
    return f"http://{host}:{port}"


def _lbc_cdp_ready(config: dict) -> bool:
    try:
        r = requests.get(_lbc_cdp_url(config) + "/json/version", timeout=0.75)
        return r.status_code == 200 and "webSocketDebuggerUrl" in r.text
    except Exception:
        return False


def _start_external_lbc_browser(url: str, config: dict) -> tuple[str, Path]:
    """Lance Brave/Chromium directement comme une application Windows.

    On ne passe que les deux paramètres indispensables au rattachement :
    - un profil dédié persistant ;
    - un port DevTools local.

    Si cette instance existe déjà, elle est simplement réutilisée.
    """
    exe, label = _resolve_external_browser(config)
    profile_dir = BASE_DIR / str(config.get("leboncoin_browser_profile_dir", "browser_profile_lbc_external"))
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = int(config.get("leboncoin_cdp_port", 9222))

    if _lbc_cdp_ready(config):
        print(f"[LBC-EXTERNAL] instance existante détectée sur 127.0.0.1:{port}")
        return label, exe

    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        url,
    ]
    print(f"[LBC-EXTERNAL] lancement Windows : {label} -> {exe}")
    print(f"[LBC-EXTERNAL] profil : {profile_dir}")
    try:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Impossible de lancer {label}: {exc}") from exc

    deadline = time.time() + float(config.get("leboncoin_cdp_start_timeout_seconds", 20))
    while time.time() < deadline:
        if _lbc_cdp_ready(config):
            print(f"[LBC-EXTERNAL] DevTools disponible sur 127.0.0.1:{port}")
            return label, exe
        time.sleep(0.4)
    raise RuntimeError(f"{label} s'est lancé mais le port DevTools {port} ne répond pas")


def _find_lbc_page(context, search_url: str, search_id: str | None = None):
    """Réutilise l'onglet LBC déjà ouvert, sinon crée un onglet dans le même Brave."""
    for page in context.pages:
        try:
            host = urlparse(page.url).hostname or ""
            if host in SUPPORTED_HOSTS["leboncoin"] and page.url == search_url:
                if search_id is not None:
                    owner = page.evaluate("() => window.name")
                    if owner and owner != "market:" + search_id:
                        continue
                    page.evaluate("name => { window.name = name; }", "market:" + search_id)
                return page
        except Exception:
            pass
    page = context.new_page()
    if search_id is not None:
        page.evaluate("name => { window.name = name; }", "market:" + search_id)
    page.goto(search_url, wait_until="domcontentloaded", timeout=40_000)
    return page


def _attach_external_lbc_browser(p, search_url: str, config: dict, search_id: str | None = None):
    with LBC_BROWSER_LOCK:
        label, exe = _start_external_lbc_browser(search_url, config)
    cdp = _lbc_cdp_url(config)
    print(f"[LBC-EXTERNAL] rattachement Playwright -> {cdp}")
    browser = p.chromium.connect_over_cdp(cdp, timeout=int(config.get("leboncoin_cdp_connect_timeout_seconds", 15)) * 1000)
    if not browser.contexts:
        raise RuntimeError("Brave est attaché mais aucun contexte navigateur n'est disponible")
    context = browser.contexts[0]
    page = _find_lbc_page(context, search_url, search_id)
    print(f"[LBC-EXTERNAL] attaché à {label}; Playwright n'a pas lancé le navigateur")
    return browser, context, page, label, exe


def _fetch_leboncoin_with_browser(url: str, config: dict, *, interactive: bool = False) -> tuple[int, str]:
    """Lit Leboncoin dans l'instance Brave externe déjà ouverte.

    Cette fonction ne ferme jamais Brave. En interactif, elle attend simplement
    que l'utilisateur résolve une éventuelle vérification dans la fenêtre réelle.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright n'est pas installé")
    timeout_ms = int(config.get("leboncoin_browser_timeout_seconds", 40)) * 1000
    poll_ms = int(config.get("leboncoin_manual_poll_ms", 1500))

    with sync_playwright() as p:
        browser, context, page, _label, _exe = _attach_external_lbc_browser(p, url, config)
        status = 0
        if page.url != url:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = int(response.status) if response is not None else 0
        else:
            try:
                response = page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                status = int(response.status) if response is not None else 0
            except PlaywrightTimeoutError:
                status = 0

        if interactive:
            try:
                page.bring_to_front()
            except Exception:
                pass
            deadline = time.time() + int(config.get("leboncoin_verify_timeout_seconds", 180))
            while time.time() < deadline:
                source = page.content()
                if parse_leboncoin(source):
                    return status or 200, source
                page.wait_for_timeout(poll_ms)
        else:
            page.wait_for_timeout(int(config.get("leboncoin_browser_wait_ms", 3000)))
        return status, page.content()


def _show_lbc_page(context, page):
    """Restore the actual Chromium window before focusing its tab."""
    session = None
    try:
        session = context.new_cdp_session(page)
        window = session.send("Browser.getWindowForTarget")
        session.send("Browser.setWindowBounds", {
            "windowId": window["windowId"], "bounds": {"windowState": "normal"},
        })
    except Exception as exc:
        print(f"[LBC-EXTERNAL] restauration fenêtre impossible : {type(exc).__name__}")
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
    try:
        page.bring_to_front()
    except Exception:
        pass


def lbc_paused(search: dict) -> bool:
    return search.get("site") == "leboncoin" and LBC_CAPTCHA_OWNER not in (None, search["id"])


def _wait_lbc_turn(search: dict) -> bool:
    while search_is_active(search):
        if not lbc_paused(search) and LBC_CYCLE_LOCK.acquire(timeout=0.2):
            if not lbc_paused(search):
                return True
            LBC_CYCLE_LOCK.release()
        STOP_EVENT.wait(0.2)
    return False


def _label_lbc_page(page, search):
    try:
        page.evaluate("label => { document.title = label; }", f"Market · {search['name']} [{search['id']}]")
    except Exception:
        pass


def _lbc_manual_verification_worker(search: dict, config: dict) -> None:
    """Surveille LBC dans une instance Brave lancée extérieurement par Windows.

    Brave reste ouvert entre les contrôles. Playwright s'attache via CDP et ne
    crée pas le processus navigateur. En cas de CAPTCHA/restriction, l'onglet est
    laissé visible jusqu'à l'intervention de l'utilisateur.
    """
    global LBC_CAPTCHA_OWNER
    search_id = search["id"]
    challenge_notified = LBC_CAPTCHA_OWNER == search_id
    try:
        if sync_playwright is None:
            raise RuntimeError("Playwright n'est pas installé")
        poll_ms = int(config.get("leboncoin_manual_poll_ms", 1500))
        nav_timeout_ms = int(config.get("leboncoin_browser_timeout_seconds", 40)) * 1000

        print(f"[LBC-EXTERNAL] {search_id} : préparation de Brave externe")
        with sync_playwright() as p:
            if not _wait_lbc_turn(search):
                return
            try:
                browser, context, page, browser_label, browser_exe = _attach_external_lbc_browser(p, search["url"], config, search_id)
                _label_lbc_page(page, search)
            finally:
                LBC_CYCLE_LOCK.release()
            print(f"[LBC-EXTERNAL] {search_id} : navigateur utilisé : {browser_label} ({browser_exe})")
            first_cycle = True

            while search_is_active(search):
                if not _wait_lbc_turn(search):
                    break
                try:
                    if page.is_closed():
                        # L'utilisateur a peut-être fermé seulement l'onglet : en reprendre/créer un.
                        page = _find_lbc_page(context, search["url"], search_id)

                    # Au premier passage on NE recharge PAS : Brave vient d'être lancé avec
                    # l'URL exactement comme si elle avait été ouverte depuis Windows. Cela évite
                    # deux navigations rapprochées. Les cycles suivants font seulement un F5.
                    if first_cycle:
                        print(f"[LBC-EXTERNAL] {search_id} : première page laissée telle quelle (aucun double chargement)")
                        status = 0
                        first_cycle = False
                        page.wait_for_timeout(int(config.get("leboncoin_initial_settle_ms", 4000)))
                    else:
                        try:
                            if page.url and "leboncoin.fr" in page.url:
                                print(f"[LBC-EXTERNAL] {search_id} : F5 de l'onglet existant")
                                response = page.reload(wait_until="domcontentloaded", timeout=nav_timeout_ms)
                            else:
                                print(f"[LBC-EXTERNAL] {search_id} : retour sur la recherche")
                                response = page.goto(search["url"], wait_until="domcontentloaded", timeout=nav_timeout_ms)
                            status = int(response.status) if response is not None else 0
                        except PlaywrightTimeoutError:
                            status = 0

                    while search_is_active(search):
                        if page.is_closed():
                            raise RuntimeError("L'onglet Leboncoin a été fermé pendant la vérification.")
                        try:
                            source = page.content()
                            items = parse_leboncoin(source)
                        except Exception:
                            source = ""
                            items = []

                        blocked = not items and looks_blocked(source)
                        normal_page = any(marker in source.lower() for marker in (
                            "__next_data__", "aucune annonce", "aucun résultat", "aucun resultat"))
                        if items or (normal_page and not blocked):
                            if challenge_notified:
                                LBC_CAPTCHA_OWNER = None
                            if challenge_notified:
                                print(f"[LBC-EXTERNAL] {search_id} : vérification résolue, {len(items)} annonce(s) détectée(s)")
                            else:
                                print(f"[LBC-EXTERNAL] {search_id} : {len(items)} annonce(s) détectée(s)")
                            _label_lbc_page(page, search)
                            _submit_to_main_loop(_process_lbc_persistent_items(search, items, recovered=challenge_notified))
                            challenge_notified = False
                            break

                        if blocked and not challenge_notified:
                            LBC_CAPTCHA_OWNER = search_id
                            _label_lbc_page(page, search)
                            print(f"[HUMAN] {search_id} : vérification/restriction détectée (HTTP {status or 'n/a'}), Brave reste ouvert")
                            _show_lbc_page(context, page)
                            _submit_to_main_loop(_notify_lbc_verification_required(
                                search, challenge=True))
                            challenge_notified = True

                        if not blocked and not challenge_notified:
                            # Unknown/empty pages are local diagnostics, never a public alert.
                            print(f"[LBC-EXTERNAL] {search_id} : page sans annonce lisible ; prochain contrôle normal")
                            break
                        page.wait_for_timeout(poll_ms)

                finally:
                    LBC_CYCLE_LOCK.release()

                if not search_is_active(search):
                    break
                current = get_search(search_id)
                min_m, max_m = validate_interval(current["min_minutes"], current["max_minutes"])
                delay = random.uniform(min_m * 60, max_m * 60)
                print(f"[LBC-EXTERNAL] {search_id} : prochain F5 dans {delay/60:.1f} min ; Brave reste ouvert")
                remaining = delay
                while remaining > 0 and search_is_active(search):
                    if not _lbc_cdp_ready(config):
                        raise RuntimeError("Brave externe a été fermé ou le port DevTools n'est plus disponible.")
                    step = min(5.0, remaining)
                    page.wait_for_timeout(int(step * 1000))
                    remaining -= step
    except Exception as exc:
        print(f"[LBC-EXTERNAL] {search_id} : {type(exc).__name__}: {exc}")
        _submit_to_main_loop(_fail_lbc_verification(search, f"{type(exc).__name__}: {str(exc)[:500]}"))
    finally:
        if LBC_CAPTCHA_OWNER == search_id and not search_is_active(search):
            LBC_CAPTCHA_OWNER = None
        with LBC_VERIFICATION_LOCK:
            LBC_VERIFICATION_ACTIVE.discard(search_id)


def _start_lbc_manual_verification(search: dict, config: dict) -> bool:
    """Démarre au plus un worker LBC externe par recherche."""
    search_id = search["id"]
    with LBC_VERIFICATION_LOCK:
        if search_id in LBC_VERIFICATION_ACTIVE:
            return False
        LBC_VERIFICATION_ACTIVE.add(search_id)
    thread = threading.Thread(
        target=_lbc_manual_verification_worker,
        args=(dict(search), dict(config)),
        name=f"lbc-external-{search_id}",
        daemon=True,
    )
    thread.start()
    return True

def _browser_leboncoin_items(search: dict, config: dict, *, interactive: bool = False) -> list[Listing]:
    status, body = _fetch_leboncoin_with_browser(search["url"], config, interactive=interactive)
    mode = "interactif" if interactive else "browser"
    print(f"[HTTP] {search['id']} : {mode} -> HTTP {status}, {len(body):,} octets")
    items = parse_leboncoin(body)
    if items:
        print(f"[PARSE] {search['id']} : {len(items)} annonce(s) via {mode}")
        return items
    if status in (403, 429) or looks_blocked(body):
        raise FetchBlocked(f"Leboncoin refuse encore le navigateur ({'HTTP '+str(status) if status else 'challenge'})", status or None)
    if len(body) > 20_000:
        raise RuntimeError(f"0 annonce Leboncoin parsée via navigateur malgré {len(body):,} octets")
    return []


def fetch_search(search: dict, config: dict) -> list[Listing]:
    if search.get("site") == "leboncoin":
        with LBC_VERIFICATION_LOCK:
            already_active = search.get("id") in LBC_VERIFICATION_ACTIVE
        if already_active:
            raise HumanVerificationPending("Session Brave externe Leboncoin déjà active")
        # V2.7 : par défaut on évite même les requêtes HTTP 403 qui précédaient
        # le navigateur. Leboncoin est lu directement dans Brave lancé par Windows.
        if config.get("leboncoin_external_browser_only", True):
            started = _start_lbc_manual_verification(search, config)
            if started:
                print(f"[LBC-EXTERNAL] {search['id']} : mode navigateur externe direct lancé (aucun requests/curl préalable)")
            raise HumanVerificationPending("Session Brave externe Leboncoin active")
    headers = {
        "User-Agent": config["user_agent"],
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    timeout = int(config.get("request_timeout_seconds", 25))
    site = search["site"]

    # Leboncoin refuse souvent requests alors que la même URL fonctionne dans
    # Chrome. On tente donc le transport navigateur curl_cffi en priorité.
    # Vinted reste d'abord sur requests car la V1 fonctionnait déjà ainsi.
    transports = []
    if site == "leboncoin" and config.get("use_curl_cffi", True):
        transports.append("curl")
        transports.append("requests")
    else:
        transports.append("requests")
        if config.get("use_curl_cffi_fallback", True):
            transports.append("curl")

    last_block: FetchBlocked | None = None
    last_error: Exception | None = None

    for transport in transports:
        if transport == "curl" and curl_requests is None:
            print(f"[HTTP] {search['id']} : curl_cffi indisponible, fallback requests")
            continue
        try:
            if transport == "curl":
                response = _request_with_curl(
                    search["url"], headers, timeout, str(config.get("curl_impersonate", "chrome"))
                )
            else:
                response = _request_with_requests(search["url"], headers, timeout)

            status = int(response.status_code)
            body = response.text
            print(f"[HTTP] {search['id']} : {transport} -> HTTP {status}, {len(body):,} octets")

            # Même si la page contient les scripts DataDome/CAPTCHA habituels,
            # elle est valide dès lors que les annonces sont réellement là.
            items = parse_vinted(body) if site == "vinted" else parse_leboncoin(body)
            if items:
                print(f"[PARSE] {search['id']} : {len(items)} annonce(s) via {transport}")
                return items

            if status in (403, 429):
                last_block = FetchBlocked(f"HTTP {status} via {transport}", status)
                continue

            if status >= 400:
                try:
                    response.raise_for_status()
                except Exception as exc:
                    last_error = exc
                    continue

            if looks_blocked(body):
                last_block = FetchBlocked(f"Page de challenge/CAPTCHA via {transport}", status)
                continue

            if len(body) > 20_000:
                last_error = RuntimeError(
                    f"0 annonce parsée via {transport} malgré une page de {len(body):,} octets : structure modifiée possible"
                )
                continue

            # Petite page sans item : peut être une vraie recherche vide.
            return []

        except FetchBlocked as exc:
            last_block = exc
        except Exception as exc:
            last_error = exc
            print(f"[HTTP] {search['id']} : {transport} a échoué : {type(exc).__name__}: {exc}")

    # Dernier recours Leboncoin : on démarre directement UNE session navigateur
    # persistante. On ne fait plus un premier lancement navigateur jetable suivi
    # d'un second lancement pour le CAPTCHA : c'était la cause des deux fenêtres
    # ouvertes à quelques secondes d'intervalle.
    if site == "leboncoin" and config.get("leboncoin_browser_fallback", True):
        if config.get("leboncoin_wait_for_manual_verification", True):
            started = _start_lbc_manual_verification(search, config)
            if started:
                print(f"[LBC-BROWSER] {search['id']} : session persistante lancée")
            raise HumanVerificationPending("Session navigateur Leboncoin persistante active")

    if last_block is not None:
        raise last_block
    if last_error is not None:
        raise last_error
    raise RuntimeError("Aucun transport HTTP disponible")


def validate_interval(min_minutes: float, max_minutes: float) -> tuple[float, float]:
    min_m = float(min_minutes)
    max_m = float(max_minutes)
    if not math.isfinite(min_m) or not math.isfinite(max_m):
        raise ValueError('Les intervalles doivent être des nombres finis.')
    if min_m <= 0 or max_m <= 0:
        raise ValueError("Les intervalles doivent être > 0 minute.")
    if min_m > max_m:
        raise ValueError("min_minutes doit être <= max_minutes.")
    if min_m < 0.25:
        raise ValueError("Minimum autorisé : 0,25 minute (15 secondes).")
    if max_m > 24 * 60:
        raise ValueError("Maximum autorisé : 1440 minutes (24 h).")
    return min_m, max_m


def unique_search_id(site: str, searches: list[dict]) -> str:
    prefix = "vinted" if site == "vinted" else "leboncoin"
    used = {s["id"] for s in searches}
    i = 1
    while f"{prefix}-{i}" in used:
        i += 1
    return f"{prefix}-{i}"


def state_for(state: dict, search_id: str) -> dict:
    searches_state = state.setdefault("searches", {})
    return searches_state.setdefault(search_id, {
        "initialized": False,
        "seen_ids": [],
        "consecutive_failures": 0,
        "blocked": False,
        "last_error": None,
        "last_success": None,
        "last_check": None,
    })


def remember_items(items: Iterable[Listing], search_state: dict) -> list[Listing]:
    known = set(str(x) for x in search_state.get("seen_ids", []))
    new_items = [item for item in items if item.item_id not in known]
    for item in items:
        known.add(item.item_id)
    # Insertion-order-ish persistence while bounding growth.
    search_state["seen_ids"] = list(known)[-8000:]
    return new_items


DISCORD_CHANNEL_ID = 0
CONFIG, STATE, SEARCHES = {}, {"searches": {}}, []
client = None
next_due: dict[str, float] = {}
search_lock = asyncio.Lock()


def search_is_active(search: dict) -> bool:
    current = get_search(search["id"])
    return bool(not STOP_EVENT.is_set() and current and current.get("enabled", True)
                and ACCESS and ACCESS.allowed(current.get("owner_id", 0)))


def get_search(search_id: str) -> dict | None:
    return next((s for s in SEARCHES if s.get("id") == search_id), None)


def schedule_next(search: dict, extra_seconds: float = 0, *, reason: str = "intervalle normal") -> float:
    min_m, max_m = validate_interval(search["min_minutes"], search["max_minutes"])
    seconds = random.uniform(min_m * 60, max_m * 60) + extra_seconds
    next_due[search["id"]] = time.monotonic() + seconds
    print(f"[SCHED] {search['id']} : prochain contrôle dans {seconds/60:.1f} min ({reason})")
    return seconds


def schedule_after(search: dict, seconds: float, *, reason: str) -> float:
    next_due[search["id"]] = time.monotonic() + seconds
    print(f"[SCHED] {search['id']} : prochain contrôle dans {seconds/60:.1f} min ({reason})")
    return seconds


def due_in_minutes(search_id: str) -> float | None:
    due = next_due.get(search_id)
    if due is None:
        return None
    return max(0.0, (due - time.monotonic()) / 60.0)


def backoff_seconds(failures: int) -> int:
    base = int(CONFIG.get("block_backoff_base_minutes", 15)) * 60
    cap = int(CONFIG.get("block_backoff_max_minutes", 120)) * 60
    # 1st/2nd refusal: base; from 3rd, exponential circuit breaker.
    exponent = max(0, failures - 3)
    return min(cap, base * (2 ** exponent))


async def notification_channel() -> discord.TextChannel | None:
    channel = client.get_channel(DISCORD_CHANNEL_ID)
    return channel if isinstance(channel, discord.TextChannel) else None


async def send_listing(channel: discord.TextChannel, search: dict, item: Listing) -> None:
    if not search_is_active(search):
        return
    site_label = "Vinted" if item.site == "vinted" else "Leboncoin"
    embed = discord.Embed(
        title=item.title[:256] or "Nouvelle annonce",
        url=item.url,
        description=f"💶 **{item.display_price}**" + (f"\n📍 {item.location}" if item.location else ""),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=f"🆕 Nouvelle annonce {site_label}")
    if item.image_url:
        embed.set_thumbnail(url=item.image_url)
    embed.set_footer(text=f"{search['name']} • ID {item.item_id}")
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def send_system_alert(channel: discord.TextChannel, search: dict, title: str, message: str, warning: bool = True) -> None:
    if not search_is_active(search):
        return
    embed = discord.Embed(
        title=("⚠️ " if warning else "✅ ") + title,
        description=message,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Recherche : {search['name']} ({search['id']})")
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _notify_lbc_verification_required(search: dict, *, challenge=True) -> None:
    if not challenge:
        return
    if not search_is_active(search):
        return
    channel = await notification_channel()
    if channel is None:
        return
    embed = discord.Embed(
        title="🧩 Vérification Leboncoin requise" if challenge else "⚠️ Page Leboncoin à contrôler",
        description=(
            ("Leboncoin semble demander une vérification manuelle. " if challenge else
             "Aucune annonce lisible : recherche vide, page en chargement ou lecture impossible ; ce n’est pas forcément un CAPTCHA. ")
            + "Le bot a demandé l’affichage de la fenêtre Brave/Chrome/Edge sur le PC qui exécute Market. "
            "Vérifie la barre des tâches et la session Windows de ce PC (pas celle du téléphone ou d’un autre PC).\n\n"
            "Résous simplement la vérification dans cette fenêtre. Le bot surveille la page en arrière-plan et "
            "reprendra automatiquement dès que les annonces seront de nouveau accessibles.\n\n"
            "Toutes les autres recherches Leboncoin sont en pause jusqu’à résolution. Vinted continue de fonctionner."
        ),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Recherche : {search['name']} ({search['id']})")
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _finish_lbc_verification(search: dict, items: list[Listing]) -> None:
    if not search_is_active(search):
        return
    """Finalise sur la boucle asyncio après résolution manuelle du challenge."""
    sstate = state_for(STATE, search["id"])
    was_initialized = bool(sstate.get("initialized", False))
    if not was_initialized:
        sstate["seen_ids"] = [item.item_id for item in items]
        sstate["initialized"] = True
        new_items: list[Listing] = []
    else:
        new_items = remember_items(items, sstate)
    sstate["consecutive_failures"] = 0
    sstate["blocked"] = False
    sstate["waiting_human_verification"] = False
    sstate["last_error"] = None
    sstate["last_success"] = now_iso()
    save_json(STATE_PATH, STATE)

    channel = await notification_channel()
    if channel is not None:
        await send_system_alert(
            channel,
            search,
            "Vérification Leboncoin terminée",
            f"La page est de nouveau accessible : **{len(items)} annonce(s)** détectée(s). Surveillance normale reprise.",
            warning=False,
        )
        for item in new_items:
            if not search_is_active(search):
                break
            print(f"[NEW] {search['id']} | {item.display_price} | {item.title}")
            await send_listing(channel, search, item)
    schedule_next(search, reason="après vérification humaine")


async def _process_lbc_persistent_items(search: dict, items: list[Listing], *, recovered: bool = False) -> None:
    if not search_is_active(search):
        return
    """Traite un cycle réussi provenant de la session navigateur LBC persistante."""
    sstate = state_for(STATE, search["id"])
    if not sstate.get("initialized", False):
        sstate["seen_ids"] = [item.item_id for item in items]
        sstate["initialized"] = True
        new_items: list[Listing] = []
        print(f"[INFO] {search['id']} initialisée sans notification ({len(items)} IDs) via navigateur persistant.")
    else:
        new_items = remember_items(items, sstate)

    sstate["consecutive_failures"] = 0
    sstate["blocked"] = False
    sstate["waiting_human_verification"] = False
    sstate["last_error"] = None
    sstate["last_success"] = now_iso()
    save_json(STATE_PATH, STATE)

    channel = await notification_channel()
    if channel is not None:
        if recovered:
            await send_system_alert(
                channel, search, "Vérification Leboncoin terminée",
                f"La session Brave est de nouveau accessible : **{len(items)} annonce(s)** détectée(s). Elle reste ouverte pour les prochains contrôles.",
                warning=False,
            )
        for item in new_items:
            if not search_is_active(search):
                break
            print(f"[NEW] {search['id']} | {item.display_price} | {item.title}")
            await send_listing(channel, search, item)


async def _fail_lbc_verification(search: dict, message: str) -> None:
    if not search_is_active(search):
        return
    sstate = state_for(STATE, search["id"])
    sstate["waiting_human_verification"] = False
    sstate["last_error"] = message
    save_json(STATE_PATH, STATE)
    channel = await notification_channel()
    if channel is not None:
        await send_system_alert(
            channel,
            search,
            "Vérification Leboncoin interrompue",
            "La session navigateur a été interrompue. Le bot réessaiera plus tard ; les détails sont dans les logs Market.",
        )
    schedule_after(search, int(CONFIG.get("block_backoff_base_minutes", 15)) * 60, reason="vérification interrompue")


def _submit_to_main_loop(coro) -> None:
    loop = MAIN_LOOP
    if loop is None or not loop.is_running():
        try:
            coro.close()
        except Exception:
            pass
        return
    asyncio.run_coroutine_threadsafe(coro, loop)


async def run_search(search: dict, *, manual: bool = False) -> tuple[bool, str]:
    if not search_is_active(search):
        return False, "Recherche suspendue ou accès retiré."
    if lbc_paused(search):
        return False, "Leboncoin en pause : résous la vérification dans l’onglet Market concerné."
    channel = await notification_channel()
    if channel is None:
        return False, "Salon Discord introuvable"

    sstate = state_for(STATE, search["id"])
    sstate["last_check"] = now_iso()
    was_blocked = bool(sstate.get("blocked", False))

    try:
        print(f"[CHECK] {search['id']} : contrôle de {search['site']}…")
        items = await asyncio.to_thread(fetch_search, search, CONFIG)
        if not search_is_active(search):
            return False, "Recherche suspendue ou accès retiré."
        print(f"[OK] {search['id']} : {len(items)} annonce(s) récupérée(s)")

        if not sstate.get("initialized", False):
            sstate["seen_ids"] = [item.item_id for item in items]
            sstate["initialized"] = True
            new_items: list[Listing] = []
            print(f"[INFO] {search['id']} initialisée sans notification ({len(items)} IDs).")
        else:
            new_items = remember_items(items, sstate)

        sstate["consecutive_failures"] = 0
        sstate["blocked"] = False
        sstate["last_error"] = None
        sstate["last_success"] = now_iso()
        save_json(STATE_PATH, STATE)

        if was_blocked and CONFIG.get("notify_recovery", True):
            await send_system_alert(channel, search, "Accès rétabli", "La recherche répond à nouveau normalement.", warning=False)

        for item in new_items:
            if not search_is_active(search):
                break
            print(f"[NEW] {search['id']} | {item.display_price} | {item.title}")
            await send_listing(channel, search, item)

        if manual:
            return True, f"{len(items)} annonce(s), {len(new_items)} nouvelle(s)"
        return True, "ok"

    except HumanVerificationPending as exc:
        sstate["blocked"] = True
        sstate["waiting_human_verification"] = True
        sstate["last_error"] = str(exc)
        save_json(STATE_PATH, STATE)
        # La session Brave externe vit dans son propre thread : ne bloque pas Vinted ni les autres recherches.
        delay = int(CONFIG.get("leboncoin_pending_recheck_minutes", 30)) * 60
        schedule_after(search, delay, reason="attente vérification humaine")
        print(f"[HUMAN] {search['id']} : {exc}")
        return False, f"Échec {type(exc).__name__} ; détails dans les logs Market."

    except FetchBlocked as exc:
        failures = int(sstate.get("consecutive_failures", 0)) + 1
        sstate["consecutive_failures"] = failures
        sstate["blocked"] = True
        sstate["last_error"] = str(exc)
        save_json(STATE_PATH, STATE)
        cooldown = backoff_seconds(failures)

        # Une seule alerte lors du passage OK -> bloqué, puis rappel si le blocage persiste très longtemps.
        should_alert = not was_blocked or failures in (3, 5, 7)
        if should_alert:
            await send_system_alert(
                channel,
                search,
                f"Blocage détecté sur {'Vinted' if search['site']=='vinted' else 'Leboncoin'}",
                f"{exc}. Le bot n'insiste pas et met cette recherche en pause pendant environ **{cooldown // 60} min**. "
                f"Échecs consécutifs : **{failures}**.",
            )
        print(f"[BLOCK] {search['id']} : {exc} | échec #{failures} | cooldown {cooldown//60} min")
        schedule_after(search, cooldown, reason=f"blocage, échec #{failures}")
        return False, f"bloqué ({exc}), cooldown {cooldown//60} min"

    except requests.HTTPError as exc:
        failures = int(sstate.get("consecutive_failures", 0)) + 1
        sstate["consecutive_failures"] = failures
        status = exc.response.status_code if exc.response is not None else "?"
        sstate["last_error"] = f"HTTP {status}: {exc}"
        save_json(STATE_PATH, STATE)
        # Erreurs serveur : backoff léger sans les traiter comme un bannissement.
        delay = min(30 * 60, max(5 * 60, failures * 5 * 60))
        print(f"[HTTP] {search['id']} : HTTP {status} | échec #{failures}")
        schedule_after(search, delay, reason=f"erreur HTTP {status}")
        return False, f"HTTP {status}, nouvel essai plus tard"

    except Exception as exc:
        failures = int(sstate.get("consecutive_failures", 0)) + 1
        sstate["consecutive_failures"] = failures
        sstate["last_error"] = f"{type(exc).__name__}: {exc}"
        save_json(STATE_PATH, STATE)
        print(f"[ERREUR] {search['id']} : {type(exc).__name__}: {exc}")
        # Au 3e échec de parsing/réseau, avertir Discord sans spammer.
        if failures == 3:
            await send_system_alert(channel, search, "Erreurs répétées", "Échecs réseau ou de lecture répétés. Consulte les logs Market ; le bot continue avec un rythme ralenti.")
        delay = min(30 * 60, max(3 * 60, failures * 3 * 60))
        schedule_after(search, delay, reason=f"erreur {type(exc).__name__}")
        return False, str(exc)


async def scheduler() -> None:
    await client.wait_until_ready()
    for search in SEARCHES:
        if search.get("enabled", True):
            # Petite dispersion au démarrage, sans faire un burst sur tous les sites.
            delay = random.uniform(1, 8)
            next_due[search["id"]] = time.monotonic() + delay
            print(f"[SCHED] {search['id']} : premier contrôle dans {delay:.1f} s")

    while not client.is_closed():
        try:
            enabled = [s for s in SEARCHES if search_is_active(s) and not lbc_paused(s)]
            now = time.monotonic()
            due = sorted(enabled, key=lambda s: next_due.get(s["id"], now))
            target = next((s for s in due if next_due.get(s["id"], 0) <= now), None)
            if target:
                async with search_lock:
                    # planifie d'abord le prochain passage normal ; run_search peut le remplacer en cas d'erreur/blocage
                    schedule_next(target, reason="intervalle normal")
                    await run_search(target)
                await asyncio.sleep(float(CONFIG.get("between_searches_seconds", 4)))
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[ERREUR SCHEDULER] {type(exc).__name__}: {exc}")
            await asyncio.sleep(5)
