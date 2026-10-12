"""Ciclo notturno: raccolta -> controllo qualità -> promozione -> metriche -> avvisi -> backup."""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import backup, booking_parse as bp, classify, db, geo, notify, quality, schedule
from .config import Config
from .scraper import BlockedError, BookingScraper, SearchOutcome, pause

log = logging.getLogger(__name__)


def today_local(cfg: Config) -> date:
    return datetime.now(ZoneInfo(cfg.raw.get("timezone", "Europe/Rome"))).date()


# --- anagrafica -----------------------------------------------------------------------

def own_booking_id(conn, cfg: Config) -> str | None:
    if cfg.own.get("booking_id"):
        return str(cfg.own["booking_id"]).strip("/")
    row = conn.execute("SELECT booking_id FROM properties WHERE is_own LIMIT 1").fetchone()
    return row["booking_id"] if row else None


def ensure_own(conn, cfg: Config, booking_id: str) -> None:
    conn.execute(
        """INSERT INTO properties (booking_id, name, url, is_own, segment)
           VALUES (%s, %s, %s, true, 'apartment')
           ON CONFLICT (booking_id) DO UPDATE SET is_own = true""",
        (booking_id, cfg.own.get("name"), bp.canonical_url(booking_id)),
    )
    conn.execute("UPDATE properties SET is_own = false WHERE is_own AND booking_id <> %s", (booking_id,))
    conn.commit()


def own_point(conn, cfg: Config) -> tuple[float, float]:
    row = conn.execute("SELECT lat, lon FROM properties WHERE is_own AND lat IS NOT NULL LIMIT 1").fetchone()
    return (row["lat"], row["lon"]) if row else cfg.own_point


def reclassify_all(conn, cfg: Config) -> None:
    """Ricalcola poligono, distanza e segmento dai dati salvati (dopo un cambio di configurazione)."""
    olat, olon = own_point(conn, cfg)
    rules = cfg.section("classification")
    rows = conn.execute(
        "SELECT booking_id, name, type_id, type_label, unit_text, lat, lon, is_own, segment FROM properties"
    ).fetchall()
    for r in rows:
        in_poly = dist = None
        if r["lat"] is not None:
            in_poly = geo.point_in_polygon(r["lat"], r["lon"], cfg.polygon)
            dist = round(geo.haversine_m(olat, olon, r["lat"], r["lon"]), 1)
        seg = "apartment" if r["is_own"] else classify.segment_for(
            r["booking_id"], r["type_id"], r["type_label"], r["unit_text"], r["name"], rules=rules)
        conn.execute(
            "UPDATE properties SET in_polygon = %s, distance_m = %s, segment = %s WHERE booking_id = %s",
            (in_poly, dist, seg, r["booking_id"]),
        )
    conn.commit()


def register_cards(conn, cards: list[bp.Card]) -> int:
    new = 0
    for c in cards:
        cur = conn.execute(
            """INSERT INTO properties (booking_id, name, url, unit_text)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (booking_id) DO UPDATE
                 SET name = EXCLUDED.name,
                     unit_text = COALESCE(EXCLUDED.unit_text, properties.unit_text)
               RETURNING (xmax = 0) AS inserted""",
            (c.booking_id, c.name, c.url, c.unit_text),
        )
        new += bool(cur.fetchone()["inserted"])
    return new


def fetch_pending_details(conn, cfg: Config, scraper: BookingScraper, budget: int) -> int:
    """Apre una volta la scheda delle strutture nuove: coordinate e tipologia."""
    if budget <= 0:
        return 0
    rows = conn.execute(
        """SELECT booking_id, url FROM properties
           WHERE details_fetched_at IS NULL ORDER BY is_own DESC, first_seen LIMIT %s""",
        (budget,),
    ).fetchall()
    done = 0
    for r in rows:
        pause(cfg.section("politeness").get("detail_pause_s", [5, 11]))
        try:
            d = scraper.detail(r["url"] or bp.canonical_url(r["booking_id"]))
            conn.execute(
                """UPDATE properties SET lat = %s, lon = %s, type_id = %s, type_label = %s,
                          rating = COALESCE(%s, rating), review_count = COALESCE(%s, review_count),
                          details_fetched_at = now(), details_error = NULL
                   WHERE booking_id = %s""",
                (d.lat, d.lon, d.type_id, d.type_label, d.rating, d.review_count, r["booking_id"]),
            )
        except BlockedError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Scheda %s non letta: %s", r["booking_id"], exc)
            conn.execute(
                "UPDATE properties SET details_fetched_at = now(), details_error = %s WHERE booking_id = %s",
                (str(exc)[:500], r["booking_id"]),
            )
        conn.commit()
        done += 1
    return done


# --- area di appoggio --------------------------------------------------------------------

def store_search(conn, run_id: int, out: SearchOutcome) -> None:
    conn.execute(
        """INSERT INTO staging_searches (run_id, profile, checkin, pages, n_cards, complete, stop_reason, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (run_id, profile, checkin) DO UPDATE SET
             pages = EXCLUDED.pages, n_cards = EXCLUDED.n_cards, complete = EXCLUDED.complete,
             stop_reason = EXCLUDED.stop_reason, error = EXCLUDED.error""",
        (run_id, out.profile, out.checkin, out.pages, len(out.cards), out.complete, out.stop_reason, out.error),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO staging_results (run_id, profile, checkin, booking_id, name, list_position,
                   price_shown, taxes_extra, price_total, price_includes, rating, review_count,
                   breakfast, free_cancellation, sold_out, distance_text_m, unit_text)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            [(run_id, out.profile, out.checkin, c.booking_id, c.name, c.position, c.price_shown,
              c.taxes_extra, c.price_total, c.price_includes, c.rating, c.review_count, c.breakfast,
              c.free_cancellation, c.sold_out, c.distance_text_m, c.unit_text) for c in out.cards],
        )
    conn.commit()


def run_stats(conn, run_id: int) -> quality.RunStats:
    s = conn.execute(
        """SELECT count(*) AS n, count(*) FILTER (WHERE complete) AS nc,
                  bool_or(error IS NOT NULL) AS any_error
           FROM staging_searches WHERE run_id = %s""", (run_id,)).fetchone()
    c = conn.execute(
        """SELECT count(*) AS n, count(price_total) AS priced,
                  count(*) FILTER (WHERE sold_out) AS sold_out, count(DISTINCT booking_id) AS props
           FROM staging_results WHERE run_id = %s""", (run_id,)).fetchone()
    return quality.RunStats(
        n_searches=s["n"], n_searches_complete=s["nc"], n_cards=c["n"], n_cards_priced=c["priced"],
        n_cards_sold_out=c["sold_out"], n_properties=c["props"],
    )


def quality_history(conn, cfg: Config, run_id: int) -> list[float]:
    n = int(cfg.section("quality").get("history_runs", 7))
    rows = conn.execute(
        """SELECT (quality->'details'->>'cards_per_search')::float AS cps FROM runs
           WHERE status = 'valid' AND id <> %s AND quality->'details' ? 'cards_per_search'
           ORDER BY id DESC LIMIT %s""", (run_id, n)).fetchall()
    return [r["cps"] for r in rows if r["cps"] is not None]


# --- promozione e metriche ----------------------------------------------------------------

def promote(conn, cfg: Config, run_id: int, observed_on: date) -> int:
    """Copia le osservazioni di un'esecuzione valida nello storico, con le indisponibilità."""
    inactive_days = int(cfg.section("quality").get("inactive_after_days", 30))
    conn.execute(
        """UPDATE properties p SET last_seen = now(), active = true,
                  rating = COALESCE(s.rating, p.rating), review_count = COALESCE(s.review_count, p.review_count)
           FROM (SELECT DISTINCT ON (booking_id) booking_id, rating, review_count
                 FROM staging_results WHERE run_id = %s ORDER BY booking_id, scraped_at DESC) s
           WHERE p.booking_id = s.booking_id""", (run_id,))
    conn.execute(
        """UPDATE properties SET active = false
           WHERE active AND NOT is_own
             AND COALESCE(last_seen, first_seen) < now() - make_interval(days => %s)""", (inactive_days,))
    total = 0
    for key, profile in cfg.profiles.items():
        cur = conn.execute(
            """INSERT INTO observations (run_id, observed_on, profile, checkin, nights, booking_id, available,
                   price_total, price_per_night, price_includes, breakfast, free_cancellation,
                   list_position, rating, review_count)
               SELECT r.run_id, %(obs)s, r.profile, r.checkin, %(nights)s, r.booking_id,
                      r.price_total IS NOT NULL AND NOT r.sold_out,
                      r.price_total, round(r.price_total / %(nights)s, 2), r.price_includes,
                      r.breakfast, r.free_cancellation, r.list_position, r.rating, r.review_count
               FROM staging_results r
               WHERE r.run_id = %(run)s AND r.profile = %(profile)s
                 AND (r.price_total IS NOT NULL OR r.sold_out)
               ON CONFLICT DO NOTHING""",
            {"obs": observed_on, "nights": profile.nights, "run": run_id, "profile": key})
        total += cur.rowcount
        # Struttura attiva del centro assente da una ricerca completa = non disponibile.
        cur = conn.execute(
            """INSERT INTO observations (run_id, observed_on, profile, checkin, nights, booking_id, available)
               SELECT s.run_id, %(obs)s, s.profile, s.checkin, %(nights)s, p.booking_id, false
               FROM staging_searches s
               CROSS JOIN properties p
               WHERE s.run_id = %(run)s AND s.profile = %(profile)s AND s.complete
                 AND (p.is_own OR (p.active AND p.in_polygon))
                 AND NOT EXISTS (SELECT 1 FROM staging_results r
                                 WHERE r.run_id = s.run_id AND r.profile = s.profile
                                   AND r.checkin = s.checkin AND r.booking_id = p.booking_id)
               ON CONFLICT DO NOTHING""",
            {"obs": observed_on, "nights": profile.nights, "run": run_id, "profile": key})
        total += cur.rowcount
    conn.execute("UPDATE runs SET promoted = true WHERE id = %s", (run_id,))
    conn.commit()
    return total


def refresh_metrics(conn, observed_on: date | None = None) -> None:
    if observed_on is None:
        conn.execute("TRUNCATE metrics_market")
        conn.execute("INSERT INTO metrics_market SELECT * FROM market_metrics(NULL, NULL, NULL)")
    else:
        conn.execute("DELETE FROM metrics_market WHERE observed_on = %s", (observed_on,))
        conn.execute("INSERT INTO metrics_market SELECT * FROM market_metrics(NULL, %s, %s)",
                     (observed_on, observed_on))
    conn.commit()


def cleanup_debug(cfg: Config) -> None:
    keep = int(cfg.section("booking").get("debug_keep_runs", 5))
    if not cfg.debug_dir.exists():
        return
    dirs = sorted((p for p in cfg.debug_dir.glob("run_*") if p.is_dir()),
                  key=lambda p: int(p.name.split("_")[1]))
    for d in dirs[:-keep] if keep > 0 else dirs:
        shutil.rmtree(d, ignore_errors=True)


# --- esecuzione completa ---------------------------------------------------------------------

def collect(conn, cfg: Config, run_id: int, observed_on: date, scraper_cls=BookingScraper) -> dict:
    """Raccolta nell'area di appoggio. Restituisce note (blocco, errori, tempo massimo)."""
    notes: dict = {"blocked": False, "error": None, "timeout": False, "new_properties": 0}
    pcfg = cfg.section("politeness")
    deadline = time.monotonic() + float(pcfg.get("max_run_minutes", 180)) * 60
    budget = int(cfg.section("booking").get("max_new_details_per_run", 60))
    known = {r["booking_id"]: (r["lat"], r["lon"]) for r in conn.execute(
        "SELECT booking_id, lat, lon FROM properties WHERE lat IS NOT NULL").fetchall()}

    plan = [(p, d) for p in cfg.profiles.values() for d in schedule.dates_to_check(p, observed_on)]
    consecutive_errors = 0
    try:
        with scraper_cls(cfg, run_id, known) as scraper:
            own_id = own_booking_id(conn, cfg)
            if not own_id and cfg.own.get("share_url"):
                own_id = bp.booking_id_from_url(scraper.resolve(cfg.own["share_url"]))
                log.info("Identificativo del BnB ricavato dal link: %s", own_id)
            if own_id:
                ensure_own(conn, cfg, own_id)

            for i, (profile, checkin) in enumerate(plan):
                if time.monotonic() > deadline:
                    notes["timeout"] = True
                    log.warning("Tempo massimo raggiunto: %d ricerche su %d", i, len(plan))
                    break
                if i:
                    pause(pcfg.get("search_pause_s", [6, 15]))
                try:
                    out = scraper.search(profile, checkin)
                    consecutive_errors = 0
                except BlockedError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("Ricerca %s %s fallita", profile.key, checkin)
                    out = SearchOutcome(profile.key, checkin, error=str(exc)[:500])
                    consecutive_errors += 1
                store_search(conn, run_id, out)
                notes["new_properties"] += register_cards(conn, out.cards)
                conn.commit()
                if consecutive_errors >= 3:
                    notes["error"] = f"3 ricerche consecutive fallite (ultima: {out.error})"
                    break
                # Le schede nuove si aprono subito, così le ricerche successive conoscono le distanze.
                fetched = fetch_pending_details(conn, cfg, scraper, min(budget, 10))
                budget -= fetched
                for r in conn.execute("SELECT booking_id, lat, lon FROM properties WHERE lat IS NOT NULL"):
                    scraper.known_coords[r["booking_id"]] = (r["lat"], r["lon"])
    except BlockedError as exc:
        conn.rollback()
        notes["blocked"] = True
        notes["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log.exception("Raccolta interrotta")
        notes["error"] = str(exc)[:500]
    return notes


def run_nightly(cfg: Config, scraper_cls=BookingScraper, do_backup: bool = True) -> dict:
    conn = db.connect(cfg.database_url)
    db.apply_schema(conn)
    if conn.execute("SELECT 1 FROM runs WHERE quality ? 'demo' LIMIT 1").fetchone():
        log.warning("Rimozione dei dati demo prima della prima raccolta reale")
        conn.execute("TRUNCATE observations, staging_results, staging_searches, metrics_market, "
                     "notifications, runs, properties")
        conn.commit()
    observed_on = today_local(cfg)
    run_id = conn.execute("INSERT INTO runs (observed_on) VALUES (%s) RETURNING id",
                          (observed_on,)).fetchone()["id"]
    conn.commit()
    log.info("Esecuzione %s per il %s", run_id, observed_on)

    notes = collect(conn, cfg, run_id, observed_on, scraper_cls)
    reclassify_all(conn, cfg)

    stats = run_stats(conn, run_id)
    stats.blocked = notes["blocked"]
    stats.error = notes["error"]
    verdict = quality.evaluate(stats, quality_history(conn, cfg, run_id), cfg.section("quality"))
    verdict.details.update({"timeout": notes["timeout"]})
    conn.execute(
        """UPDATE runs SET finished_at = now(), status = %s, n_searches = %s, n_searches_complete = %s,
                  n_cards = %s, n_cards_priced = %s, n_cards_sold_out = %s, n_properties = %s,
                  n_new_properties = %s, quality = %s, error = %s
           WHERE id = %s""",
        (verdict.status, stats.n_searches, stats.n_searches_complete, stats.n_cards, stats.n_cards_priced,
         stats.n_cards_sold_out, stats.n_properties, notes["new_properties"],
         json.dumps({"reasons": verdict.reasons, "details": verdict.details}), notes["error"], run_id))
    conn.commit()
    log.info("Esito esecuzione %s: %s %s", run_id, verdict.status, verdict.reasons)

    if verdict.status == "valid":
        n = promote(conn, cfg, run_id, observed_on)
        refresh_metrics(conn, observed_on)
        log.info("Promosse %d osservazioni", n)
    else:
        label = {"blocked": "Blocco anti-bot", "failed": "Esecuzione fallita",
                 "suspect": "Esecuzione sospetta"}[verdict.status]
        notify.alarm(cfg, f"{label} (n. {run_id}, {observed_on}).\n" + "\n".join(f"• {r}" for r in verdict.reasons)
                     + "\nI dati restano nell'area di appoggio e non entrano nello storico.",
                     ref=f"run:{run_id}", conn=conn)

    if do_backup:
        try:
            backup.run_backup(cfg)
        except Exception as exc:  # noqa: BLE001
            log.exception("Backup fallito")
            notify.alarm(cfg, f"Backup del database fallito: {exc}", ref=f"backup:{run_id}", conn=conn)
    cleanup_debug(cfg)
    conn.close()
    return {"run_id": run_id, "status": verdict.status, "reasons": verdict.reasons}
