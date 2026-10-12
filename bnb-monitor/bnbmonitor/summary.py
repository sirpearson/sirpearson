"""Riepilogo mattutino Telegram e controllo delle esecuzioni saltate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from . import metrics, notify
from .config import Config

STATUS_LABEL = {"valid": "valida ✅", "suspect": "sospetta ⚠️", "failed": "fallita ❌",
                "blocked": "bloccata 🛑", "running": "in corso ⏳"}
WEEKDAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def _d(ts) -> str:
    ts = pd.Timestamp(ts)
    return f"{WEEKDAYS[ts.weekday()]} {ts:%d/%m}"


def build_summary(conn, cfg: Config) -> str:
    mcfg = cfg.section("metrics")
    primary = mcfg.get("primary_profile") or next(iter(cfg.profiles))
    limit = int(mcfg.get("summary_max_dates", 8))
    lines = ["☀️ Monitor BnB Trieste — riepilogo"]

    run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if run:
        lines.append(f"Ultima esecuzione: {STATUS_LABEL.get(run['status'], run['status'])}, "
                     f"{run['n_searches'] or 0} ricerche, {run['n_properties'] or 0} strutture.")
    else:
        lines.append("Nessuna esecuzione registrata.")

    df = metrics.market(conn, profile=primary)
    if df.empty:
        lines.append("Ancora nessun dato valido per le metriche.")
        return "\n".join(lines)

    ch = metrics.with_7d_change(df)
    today = pd.Timestamp(datetime.now().date())
    ch = ch[ch["checkin"] >= today]
    label = cfg.profiles[primary].label

    for seg in ("appartamenti", "camere"):
        s = ch[ch["seg_group"] == seg]
        if s.empty:
            continue
        lines.append(f"\n— {metrics.SEG_GROUP_LABELS[seg]} ({label}) —")

        drop = float(mcfg.get("availability_drop_points", 15))
        falling = s[s["avail_change_pts"] <= -drop].sort_values("avail_change_pts").head(limit)
        if not falling.empty:
            lines.append("Disponibilità in calo veloce:")
            lines += [f"  {_d(r.checkin)}: {r.availability_rate:.0%} ({r.avail_change_pts:+.0f} punti in 7 gg, "
                      f"{int(r.pickup_7d) if pd.notna(r.pickup_7d) else '?'} strutture riempite)"
                      for r in falling.itertuples()]

        move = float(mcfg.get("median_move_pct", 10))
        moved = s[s["median_change_pct"].abs() >= move].copy()
        moved = moved.reindex(moved["median_change_pct"].abs().sort_values(ascending=False).index).head(limit)
        if not moved.empty:
            lines.append("Movimenti della mediana:")
            lines += [f"  {_d(r.checkin)}: € {r.median_ppn:.0f} ({r.median_change_pct:+.0f}% in 7 gg)"
                      for r in moved.itertuples()]

        own = s[s["own_ppn"].notna() & s["p25_ppn"].notna()]
        out = own[(own["own_ppn"] < own["p25_ppn"]) | (own["own_ppn"] > own["p75_ppn"])].head(limit)
        if not out.empty:
            lines.append("Prezzo del BnB fuori dalla fascia 25°–75°:")
            for r in out.itertuples():
                side = "sotto" if r.own_ppn < r.p25_ppn else "sopra"
                lines.append(f"  {_d(r.checkin)}: € {r.own_ppn:.0f} {side} (fascia € {r.p25_ppn:.0f}–{r.p75_ppn:.0f})")

        if falling.empty and moved.empty and out.empty:
            lines.append("Nessun movimento rilevante.")
    return "\n".join(lines)


def check_missed_run(conn, cfg: Config, max_age_hours: float = 26) -> str | None:
    row = conn.execute("SELECT max(started_at) AS last FROM runs").fetchone()
    last = row["last"] if row else None
    if last is None or datetime.now(timezone.utc) - last > timedelta(hours=max_age_hours):
        when = f"l'ultima è del {last:%d/%m %H:%M}" if last else "nessuna esecuzione registrata"
        return f"Esecuzione notturna saltata: {when}."
    return None


def send_morning(conn, cfg: Config) -> None:
    missed = check_missed_run(conn, cfg)
    if missed:
        notify.alarm(cfg, missed, ref="missed", conn=conn)
    notify.send(cfg, build_summary(conn, cfg), kind="summary", ref=str(datetime.now().date()), conn=conn)
