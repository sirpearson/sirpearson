"""Assegnazione di una struttura a un segmento."""
from __future__ import annotations

SEGMENTS = ("apartment", "room_extra", "room_hotel", "other")


def segment_for(booking_id: str, type_id: int | None, *texts: str | None,
                rules: dict | None = None) -> str | None:
    """Ordine: correzione manuale, codice tipologia Booking, parole chiave nei testi.

    Restituisce None se non si riesce a classificare (la struttura resta fuori
    dalle metriche finché non si aggiunge una correzione manuale).
    """
    rules = rules or {}
    overrides = rules.get("overrides") or {}
    if booking_id in overrides:
        return overrides[booking_id]
    type_ids = {int(k): v for k, v in (rules.get("type_ids") or {}).items()}
    if type_id is not None and type_id in type_ids:
        return type_ids[type_id]
    for text in texts:
        if not text:
            continue
        low = text.lower()
        for keyword, segment in rules.get("keywords") or []:
            if str(keyword).lower() in low:
                return segment
    return None
