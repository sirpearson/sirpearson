"""Lettura delle pagine Booking (funzioni pure, senza browser: si testano su HTML salvato).

I selettori di Booking cambiano nel tempo: sono raccolti in SELECTORS e, quando una
pagina cambia struttura, il controllo qualità segnala prezzi non leggibili e le pagine
salvate in data/debug servono ad aggiornarli.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

SELECTORS = {
    "card": '[data-testid="property-card"]',
    "title": '[data-testid="title"]',
    "title_link": 'a[data-testid="title-link"]',
    "price": '[data-testid="price-and-discounted-price"]',
    "taxes": '[data-testid="taxes-and-charges"]',
    "review": '[data-testid="review-score"]',
    "distance": '[data-testid="distance"]',
    "units": '[data-testid="recommended-units"]',
    "address": '[data-testid="address"]',
}

BLOCK_MARKERS = (
    "px-captcha",
    "captcha-delivery",
    "awswaf",
    "challenge-platform",
    "sei un robot",
    "are you a robot",
    "verifica di sicurezza",
    "unusual traffic",
)

SOLD_OUT_MARKERS = (
    "non abbiamo disponibilità",
    "nessuna disponibilità",
    "non disponibile",
    "esaurit",
    "no availability",
    "sold out",
)


@dataclass
class Card:
    booking_id: str
    name: str
    url: str
    position: int
    price_shown: float | None = None
    taxes_extra: float | None = None
    taxes_included: bool | None = None
    price_total: float | None = None
    price_includes: str | None = None
    rating: float | None = None
    review_count: int | None = None
    breakfast: bool = False
    free_cancellation: bool = False
    distance_text_m: float | None = None
    unit_text: str | None = None
    sold_out: bool = False


@dataclass
class Detail:
    lat: float | None = None
    lon: float | None = None
    type_id: int | None = None
    type_label: str | None = None
    rating: float | None = None
    review_count: int | None = None
    extra: dict = field(default_factory=dict)


def search_url(base_url: str, extra_params: dict, lat: float, lon: float,
               checkin, checkout, adults: int, offset: int = 0) -> str:
    params = dict(extra_params or {})
    params.update({
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "group_adults": adults,
    })
    if offset:
        params["offset"] = offset
    return f"{base_url}?{urlencode(params)}"


_HOTEL_PATH = re.compile(r"/hotel/([a-z]{2})/([^./?#]+)")


def booking_id_from_url(url: str) -> str | None:
    """'https://www.booking.com/hotel/it/nome-struttura.it.html?...' -> 'it/nome-struttura'."""
    m = _HOTEL_PATH.search(urlparse(url).path if "://" in url else url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def canonical_url(booking_id: str) -> str:
    country, slug = booking_id.split("/", 1)
    return f"https://www.booking.com/hotel/{country}/{slug}.it.html"


def parse_amount(text: str | None) -> float | None:
    """'€ 1.234' -> 1234.0; '€ 98,50' -> 98.5 (formato italiano, ma tollera quello inglese)."""
    if not text:
        return None
    m = re.search(r"\d[\d.,   ]*", text)
    if not m:
        return None
    s = re.sub(r"[   ]", "", m.group(0)).rstrip(".,")
    if "," in s and "." in s:
        # Il separatore che compare per ultimo è quello dei decimali.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        if len(tail) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_taxes(text: str | None) -> tuple[float | None, bool | None]:
    """Restituisce (tasse extra, tasse incluse)."""
    if not text:
        return None, None
    t = text.lower()
    if "include" in t or "inclus" in t:
        return 0.0, True
    if "+" in t or "tass" in t or "tax" in t:
        amount = parse_amount(t)
        if amount is not None:
            return amount, False
    return None, None


def parse_rating(text: str | None) -> tuple[float | None, int | None]:
    if not text:
        return None, None
    score = None
    for m in re.finditer(r"(?<![\d.,])(\d{1,2}(?:[,.]\d)?)(?![\d.,])", text):
        v = float(m.group(1).replace(",", "."))
        if 1 <= v <= 10 and ("," in m.group(1) or "." in m.group(1) or v == 10):
            score = v
            break
    reviews = None
    m = re.search(r"([\d.,  ]+)\s*(?:recension|giudiz|review)", text, re.I)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        reviews = int(digits) if digits else None
    return score, reviews


def parse_distance_m(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*(km|m)\b", text.lower())
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v * 1000 if m.group(2) == "km" else v


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def is_blocked(url: str, html: str, status: int | None = None) -> bool:
    if status in (403, 405, 429):
        return True
    u = (url or "").lower()
    if "captcha" in u or "challenge" in u:
        return True
    low = (html or "").lower()
    return any(m in low for m in BLOCK_MARKERS)


def parse_search_page(html: str, start_position: int = 1, include_taxes: bool = True) -> list[Card]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[Card] = []
    for i, node in enumerate(soup.select(SELECTORS["card"])):
        link = node.select_one(SELECTORS["title_link"]) or node.find("a", href=_HOTEL_PATH)
        href = link.get("href", "") if link else ""
        bid = booking_id_from_url(href)
        if not bid:
            continue
        full_text = _text(node)
        low = full_text.lower()
        card = Card(
            booking_id=bid,
            name=_text(node.select_one(SELECTORS["title"])) or bid.split("/", 1)[1],
            url=canonical_url(bid),
            position=start_position + i,
        )
        card.price_shown = parse_amount(_text(node.select_one(SELECTORS["price"])))
        card.taxes_extra, card.taxes_included = parse_taxes(_text(node.select_one(SELECTORS["taxes"])))
        if card.price_shown is not None:
            if card.taxes_included:
                card.price_total = card.price_shown
                card.price_includes = "tasse e costi inclusi"
            elif card.taxes_extra is not None:
                if include_taxes:
                    card.price_total = round(card.price_shown + card.taxes_extra, 2)
                    card.price_includes = "tasse e costi inclusi (sommati)"
                else:
                    card.price_total = card.price_shown
                    card.price_includes = "tasse e costi esclusi"
            else:
                card.price_total = card.price_shown
                card.price_includes = "tasse non indicate"
        else:
            card.sold_out = any(m in low for m in SOLD_OUT_MARKERS)
        card.rating, card.review_count = parse_rating(_text(node.select_one(SELECTORS["review"])))
        card.breakfast = "colazione inclusa" in low or "colazione compresa" in low or "breakfast included" in low
        card.free_cancellation = "cancellazione gratuita" in low or "free cancellation" in low
        card.distance_text_m = parse_distance_m(_text(node.select_one(SELECTORS["distance"])))
        units = _text(node.select_one(SELECTORS["units"]))
        card.unit_text = units[:300] or None
        cards.append(card)
    return cards


_TYPE_ID_PATTERNS = (
    r'"accommodationTypeId"\s*:\s*"?(\d+)',
    r"b_hotel_type_id\s*[:=]\s*['\"]?(\d+)",
    r"hotel_type_id['\"]?\s*[:=]\s*['\"]?(\d+)",
    r'"ht_id"\s*:\s*"?(\d+)',
)


def parse_detail_page(html: str) -> Detail:
    soup = BeautifulSoup(html, "html.parser")
    d = Detail()

    node = soup.find(attrs={"data-atlas-latlng": True})
    if node:
        try:
            lat, lon = (float(x) for x in node["data-atlas-latlng"].split(",")[:2])
            d.lat, d.lon = lat, lon
        except ValueError:
            pass

    ld_types = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            if item.get("@type"):
                ld_types.append(str(item["@type"]))
            geo_ = item.get("geo") or {}
            if d.lat is None and geo_.get("latitude") is not None:
                d.lat, d.lon = float(geo_["latitude"]), float(geo_["longitude"])
            if d.lat is None and isinstance(item.get("hasMap"), str):
                m = re.search(r"center=(-?\d+\.\d+),(-?\d+\.\d+)", item["hasMap"])
                if m:
                    d.lat, d.lon = float(m.group(1)), float(m.group(2))
            agg = item.get("aggregateRating") or {}
            if agg.get("ratingValue") is not None:
                try:
                    d.rating = float(str(agg["ratingValue"]).replace(",", "."))
                    d.review_count = int(agg.get("reviewCount") or 0) or None
                except ValueError:
                    pass

    if d.lat is None:
        m_lat = re.search(r"b_map_center_latitude\s*[:=]\s*['\"]?(-?\d+\.\d+)", html)
        m_lon = re.search(r"b_map_center_longitude\s*[:=]\s*['\"]?(-?\d+\.\d+)", html)
        if m_lat and m_lon:
            d.lat, d.lon = float(m_lat.group(1)), float(m_lon.group(1))

    for pat in _TYPE_ID_PATTERNS:
        m = re.search(pat, html)
        if m:
            d.type_id = int(m.group(1))
            break

    label_candidates = []
    badge = soup.select_one('[data-testid="property-type-badge"]')
    if badge:
        label_candidates.append(_text(badge))
    for crumb in soup.select('[data-testid="breadcrumbs"] li, #breadcrumb li, nav[aria-label*="readcrumb"] li'):
        label_candidates.append(_text(crumb))
    label_candidates += ld_types
    d.type_label = " | ".join(c for c in label_candidates if c)[:300] or None
    d.extra["ld_types"] = ld_types
    return d
