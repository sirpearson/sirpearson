"""Pianificatore interno al container: raccolta notturna a orario variabile e riepilogo mattutino."""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, notify, pipeline, summary
from .config import Config

log = logging.getLogger(__name__)


def _at(day, hhmm: str, tz) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def next_events(cfg: Config, now: datetime) -> list[tuple[datetime, str]]:
    """Prossima raccolta (con ritardo casuale nella finestra) e prossimo riepilogo."""
    s = cfg.section("schedule")
    tz = now.tzinfo
    events = []
    for kind, key in (("run", "run_window_start"), ("summary", "summary_time")):
        t = _at(now.date(), s.get(key, "01:30" if kind == "run" else "07:30"), tz)
        if kind == "run":
            t += timedelta(seconds=random.randint(0, int(s.get("run_window_minutes", 120)) * 60))
        if t <= now:
            t = _at(now.date() + timedelta(days=1), s.get(key), tz)
            if kind == "run":
                t += timedelta(seconds=random.randint(0, int(s.get("run_window_minutes", 120)) * 60))
        events.append((t, kind))
    return sorted(events)


def loop(cfg: Config) -> None:
    tz = ZoneInfo(cfg.raw.get("timezone", "Europe/Rome"))
    conn = db.connect(cfg.database_url)
    db.apply_schema(conn)
    conn.close()
    while True:
        when, kind = next_events(cfg, datetime.now(tz))[0]
        log.info("Prossimo evento: %s alle %s", kind, when.isoformat(timespec="minutes"))
        while (remaining := (when - datetime.now(tz)).total_seconds()) > 0:
            time.sleep(min(remaining, 300))
        try:
            if kind == "run":
                pipeline.run_nightly(cfg)
            else:
                conn = db.connect(cfg.database_url)
                summary.send_morning(conn, cfg)
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.exception("Evento %s fallito", kind)
            notify.alarm(cfg, f"Errore imprevisto ({kind}): {exc}")
        time.sleep(61)
