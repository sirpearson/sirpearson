"""Dati sintetici per provare la dashboard prima che lo storico reale si accumuli.

Da usare solo su un database vuoto (o di prova): rifiuta se ci sono esecuzioni reali.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta

from . import db, geo, pipeline, schedule
from .config import Config


def _random_point_in_polygon(poly, rng):
    xs, ys = zip(*poly)
    while True:
        lon, lat = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
        if geo.point_in_polygon(lat, lon, poly):
            return lat, lon


def seed(cfg: Config, days: int = 21, n_properties: int = 90, seed_value: int = 7) -> None:
    rng = random.Random(seed_value)
    conn = db.connect(cfg.database_url)
    db.apply_schema(conn)
    real = conn.execute("SELECT count(*) AS n FROM runs WHERE NOT (quality ? 'demo')").fetchone()["n"]
    if real:
        raise SystemExit("Il database contiene esecuzioni reali: i dati demo non vengono inseriti.")
    conn.execute("TRUNCATE observations, staging_results, staging_searches, metrics_market, runs, properties CASCADE")

    olat, olon = cfg.own_point
    own_id = cfg.own.get("booking_id") or "it/elegante-appartamento-in-marina"
    conn.execute(
        """INSERT INTO properties (booking_id, name, url, segment, lat, lon, is_own, rating, review_count,
                                   details_fetched_at, last_seen)
           VALUES (%s, %s, %s, 'apartment', %s, %s, true, 9.3, 48, now(), now())""",
        (own_id, cfg.own.get("name"), "https://www.booking.com/", olat, olon))

    segs = ["apartment"] * 5 + ["room_extra"] * 3 + ["room_hotel"] * 2
    props = []
    for i in range(n_properties):
        seg = rng.choice(segs)
        lat, lon = _random_point_in_polygon(cfg.polygon, rng)
        rating = round(min(10, max(6, rng.gauss(8.6, 0.6))), 1)
        base = {"apartment": 95, "room_extra": 80, "room_hotel": 115}[seg] * (1 + (rating - 8.6) * 0.12)
        base *= rng.uniform(0.8, 1.3)
        bid = f"it/demo-{seg}-{i:03d}"
        name = {"apartment": "Appartamento", "room_extra": "B&B", "room_hotel": "Hotel"}[seg] + f" Demo {i}"
        conn.execute(
            """INSERT INTO properties (booking_id, name, url, segment, lat, lon, rating, review_count,
                                       details_fetched_at, last_seen)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())""",
            (bid, name, "https://www.booking.com/", seg, lat, lon, rating, rng.randint(5, 900)))
        props.append({"id": bid, "seg": seg, "base": base, "rating": rating,
                      "min2": seg == "apartment" and rng.random() < 0.3,
                      "breakfast": seg != "apartment" and rng.random() < 0.6})
    pipeline.reclassify_all(conn, cfg)
    conn.commit()

    start = date.today() - timedelta(days=days - 1)
    for k in range(days):
        obs = start + timedelta(days=k)
        run_id = conn.execute(
            """INSERT INTO runs (observed_on, started_at, finished_at, status, n_searches, n_searches_complete,
                                 n_cards, n_cards_priced, n_cards_sold_out, n_properties, quality, promoted)
               VALUES (%s, %s::date + time '02:10', %s::date + time '03:05', 'valid', 0, 0, 0, 0, 0, %s, %s, true)
               RETURNING id""",
            (obs, obs, obs, n_properties, json.dumps({"demo": True, "details": {"cards_per_search": 40}}))
        ).fetchone()["id"]
        rows = []
        n_cards = 0
        n_searches = 0
        for profile in cfg.profiles.values():
            for checkin in schedule.dates_to_check(profile, obs):
                n_searches += 1
                lead = (checkin - obs).days
                weekend = checkin.weekday() in (4, 5)
                season = 1 + 0.15 * math.sin(checkin.toordinal() / 58)
                p_avail = min(0.95, 0.35 + lead / 110) - (0.18 if weekend else 0) - (season - 1)
                for p in [{"id": own_id, "seg": "apartment", "base": 105, "rating": 9.3,
                           "min2": False, "breakfast": False}] + props:
                    if profile.nights == 1 and p["min2"]:
                        avail = False
                    else:
                        h = random.Random(f"{p['id']}{checkin}{profile.key}").random()
                        avail = h < p_avail + (0.02 * lead / 30) * (k / days)
                    if not avail:
                        rows.append((run_id, obs, profile.key, checkin, profile.nights, p["id"], False,
                                     None, None, None, None, None, None, None, None))
                        continue
                    n_cards += 1
                    ppn = p["base"] * season * (1.22 if weekend else 1) * (1 + max(0, 30 - lead) / 150)
                    ppn *= 1.12 if profile.nights == 1 else 1
                    ppn *= rng.uniform(0.95, 1.05)
                    total = round(ppn * profile.nights, 2)
                    rows.append((run_id, obs, profile.key, checkin, profile.nights, p["id"], True, total,
                                 round(total / profile.nights, 2), "tasse e costi inclusi", p["breakfast"],
                                 rng.random() < 0.5, None, p["rating"], None))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO observations (run_id, observed_on, profile, checkin, nights, booking_id, available,
                       price_total, price_per_night, price_includes, breakfast, free_cancellation,
                       list_position, rating, review_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        conn.execute("UPDATE runs SET n_searches = %s, n_searches_complete = %s, n_cards = %s, n_cards_priced = %s"
                     " WHERE id = %s", (n_searches, n_searches, n_cards, n_cards, run_id))
        conn.commit()
    pipeline.refresh_metrics(conn)
    conn.close()
