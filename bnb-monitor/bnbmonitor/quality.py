"""Controllo qualità di un'esecuzione: valida, sospetta o fallita."""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median


@dataclass
class RunStats:
    n_searches: int = 0
    n_searches_complete: int = 0
    n_cards: int = 0
    n_cards_priced: int = 0
    n_cards_sold_out: int = 0
    n_properties: int = 0
    blocked: bool = False
    error: str | None = None

    @property
    def cards_per_search(self) -> float:
        return self.n_cards / self.n_searches if self.n_searches else 0.0


@dataclass
class Verdict:
    status: str  # valid | suspect | failed | blocked
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def evaluate(stats: RunStats, history_cards_per_search: list[float], qcfg: dict) -> Verdict:
    if stats.blocked:
        return Verdict("blocked", ["verifica anti-bot di Booking: esecuzione interrotta"])
    if stats.error and stats.n_searches_complete == 0:
        return Verdict("failed", [f"errore: {stats.error}"])
    if stats.n_searches == 0 or stats.n_searches_complete == 0:
        return Verdict("failed", ["nessuna ricerca completata"])

    reasons: list[str] = []
    details: dict = {"cards_per_search": round(stats.cards_per_search, 2)}

    readable_base = stats.n_cards - stats.n_cards_sold_out
    price_ratio = stats.n_cards_priced / readable_base if readable_base else 0.0
    details["price_read_ratio"] = round(price_ratio, 3)
    if price_ratio < float(qcfg.get("min_price_read_ratio", 0.8)):
        reasons.append(f"prezzi leggibili solo per il {price_ratio:.0%} dei risultati (cambio pagine Booking?)")

    incomplete = 1 - stats.n_searches_complete / stats.n_searches
    details["incomplete_ratio"] = round(incomplete, 3)
    if incomplete > float(qcfg.get("max_incomplete_ratio", 0.2)):
        reasons.append(f"{incomplete:.0%} delle ricerche incomplete")

    if history_cards_per_search:
        ref = median(history_cards_per_search)
        details["reference_cards_per_search"] = round(ref, 2)
        if ref > 0 and stats.cards_per_search < float(qcfg.get("drop_threshold", 0.5)) * ref:
            reasons.append(
                f"risultati crollati: {stats.cards_per_search:.1f} per ricerca contro {ref:.1f} di riferimento"
            )

    if stats.error:
        reasons.append(f"errore durante la raccolta: {stats.error}")

    return Verdict("suspect" if reasons else "valid", reasons, details)
