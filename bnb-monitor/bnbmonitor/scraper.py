"""Navigazione su Booking con Playwright (un solo browser Chromium senza finestra)."""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import booking_parse as bp
from .config import Config, Profile
from .geo import haversine_m

log = logging.getLogger(__name__)


class BlockedError(RuntimeError):
    """Booking ha mostrato una verifica anti-bot: ci si ferma senza riprovare."""


@dataclass
class SearchOutcome:
    profile: str
    checkin: date
    cards: list[bp.Card] = field(default_factory=list)
    pages: int = 0
    complete: bool = False
    stop_reason: str = ""
    error: str | None = None


def pause(bounds) -> None:
    lo, hi = bounds
    time.sleep(random.uniform(float(lo), float(hi)))


class BookingScraper:
    def __init__(self, cfg: Config, run_id: int, known_coords: dict[str, tuple[float, float]]):
        self.cfg = cfg
        self.bcfg = cfg.section("booking")
        self.pcfg = cfg.section("politeness")
        self.run_id = run_id
        self.known_coords = known_coords
        self.debug_dir = cfg.debug_dir / f"run_{run_id}"
        self._pw = self._browser = self._context = self._page = None
        self._debug_saved = 0

    # --- ciclo di vita -------------------------------------------------------
    def __enter__(self) -> "BookingScraper":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, executable_path=os.environ.get("CHROMIUM_PATH") or None)
        self._context = self._browser.new_context(
            locale="it-IT",
            timezone_id=self.cfg.raw.get("timezone", "Europe/Rome"),
            viewport={"width": 1366, "height": 900},
        )
        self._context.set_default_timeout(float(self.pcfg.get("page_timeout_s", 45)) * 1000)
        self._page = self._context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        for obj in (self._context, self._browser):
            try:
                obj and obj.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            self._pw.stop()

    # --- utilità --------------------------------------------------------------
    def _save_debug(self, name: str, html: str, force: bool = False) -> None:
        if not force and self._debug_saved >= 6:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        (self.debug_dir / f"{name}.html").write_text(html, encoding="utf-8")
        self._debug_saved += 1

    def _load(self, url: str, wait_selector: str | None, debug_name: str) -> str:
        page = self._page
        try:
            resp = page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            # Risposta di errore senza contenuto (tipico di 403/429): per prudenza ci si ferma.
            if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc):
                raise BlockedError(f"risposta di errore da {url}") from exc
            raise
        status = resp.status if resp else None
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=15_000)
            except Exception:  # noqa: BLE001 - pagina senza risultati o cambiata
                pass
        self._dismiss_overlays()
        html = page.content()
        if bp.is_blocked(page.url, html, status):
            self._save_debug(f"blocked_{debug_name}", html, force=True)
            raise BlockedError(f"verifica anti-bot su {page.url} (HTTP {status})")
        return html

    def _dismiss_overlays(self) -> None:
        page = self._page
        for sel in ("#onetrust-reject-all-handler", 'button[aria-label*="Ignora"]'):
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    time.sleep(random.uniform(0.5, 1.5))
            except Exception:  # noqa: BLE001
                pass
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass

    def _scroll(self) -> None:
        """Scorre la pagina a scatti come una persona, per caricare tutte le schede."""
        page = self._page
        for _ in range(random.randint(4, 7)):
            page.mouse.wheel(0, random.randint(900, 1600))
            time.sleep(random.uniform(0.4, 1.2))

    # --- ricerca ------------------------------------------------------------------
    def _card_distance(self, card: bp.Card) -> float | None:
        if card.booking_id in self.known_coords:
            lat, lon = self.known_coords[card.booking_id]
            return haversine_m(self.cfg.search_point[0], self.cfg.search_point[1], lat, lon)
        return card.distance_text_m

    def search(self, profile: Profile, checkin: date) -> SearchOutcome:
        out = SearchOutcome(profile.key, checkin)
        page_size = int(self.bcfg.get("page_size", 25))
        max_pages = int(self.bcfg.get("max_pages", 8))
        lat, lon = self.cfg.search_point
        seen: set[str] = set()
        for page_no in range(max_pages):
            url = bp.search_url(
                self.bcfg["base_url"], self.bcfg.get("extra_params") or {}, lat, lon,
                checkin, checkin + timedelta(days=profile.nights), profile.adults,
                offset=page_no * page_size,
            )
            html = self._load(url, bp.SELECTORS["card"], f"{profile.key}_{checkin}_p{page_no}")
            self._scroll()
            html = self._page.content()
            out.pages += 1
            cards = bp.parse_search_page(html, start_position=len(out.cards) + 1,
                                         include_taxes=bool(self.bcfg.get("price_include_taxes", True)))
            if page_no == 0 and self._debug_saved < 2:
                self._save_debug(f"search_{profile.key}_{checkin}", html)
            new_cards = [c for c in cards if c.booking_id not in seen]
            if not new_cards:
                out.complete = True
                out.stop_reason = "fine risultati" if page_no else "nessun risultato"
                if page_no == 0 and "property-card" not in html:
                    self._save_debug(f"empty_{profile.key}_{checkin}", html, force=True)
                break
            for c in new_cards:
                seen.add(c.booking_id)
                c.position = len(out.cards) + 1
                out.cards.append(c)
            distances = [d for d in (self._card_distance(c) for c in new_cards) if d is not None]
            beyond = [d for d in distances if d > self.cfg.stop_radius_m]
            if len(distances) >= 5 and len(beyond) >= 0.8 * len(distances):
                out.complete = True
                out.stop_reason = "oltre il raggio di arresto"
                break
            if len(cards) < page_size * 0.6:
                out.complete = True
                out.stop_reason = "ultima pagina"
                break
            pause(self.pcfg.get("page_pause_s", [4, 9]))
        else:
            out.stop_reason = f"raggiunto il limite di {max_pages} pagine"
        return out

    # --- scheda struttura ----------------------------------------------------------
    def detail(self, url: str) -> bp.Detail:
        html = self._load(url, "[data-atlas-latlng], h2", f"detail_{url.rsplit('/', 1)[-1]}")
        detail = bp.parse_detail_page(html)
        if detail.lat is None:
            self._save_debug(f"detail_nocoords_{url.rsplit('/', 1)[-1]}", html)
        return detail

    def resolve(self, share_url: str) -> str:
        """Segue un link di condivisione Booking e restituisce l'URL finale."""
        self._load(share_url, None, "share")
        return self._page.url
