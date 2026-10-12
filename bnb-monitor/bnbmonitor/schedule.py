"""Quali date di check-in controllare in una notte, secondo le frequenze del profilo."""
from __future__ import annotations

from datetime import date, timedelta

from .config import Profile


def dates_to_check(profile: Profile, today: date) -> list[date]:
    """Date di check-in da cercare oggi.

    Fino a `near_days` si controlla ogni data ogni notte. Oltre, ogni data di check-in
    si controlla una notte ogni `far_every_days`: la rotazione dipende dalla data di
    check-in e da oggi, così tutti i profili controllano le stesse date nella stessa
    notte (serve ai confronti 1 notte / 2 notti) e il carico è uguale ogni notte.
    """
    out = []
    step = max(1, profile.far_every_days)
    for offset in range(profile.horizon_days):
        d = today + timedelta(days=offset)
        if offset < profile.near_days or (d.toordinal() + today.toordinal()) % step == 0:
            out.append(d)
    return out
