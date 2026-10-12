"""Test end-to-end della pipeline con uno scraper finto su un PostgreSQL vero.

Richiede TEST_DATABASE_URL (database usa e getta: viene svuotato).
"""
import os
from datetime import date, timedelta

import pytest

from bnbmonitor import booking_parse as bp, config, db, metrics, pipeline, summary
from bnbmonitor.scraper import BlockedError, SearchOutcome
from conftest import ROOT

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL non impostato")

OWN = "it/elegante-appartamento-in-marina"


def make_world(cfg):
    lat0, lon0 = cfg.search_point
    world = {}
    for i in range(12):
        seg = ["apartment", "room_extra", "room_hotel"][i % 3]
        world[f"it/p{i}"] = {"lat": lat0 + (i - 6) * 0.0002, "lon": lon0 + (i % 4 - 2) * 0.0002,
                             "type_id": {"apartment": 201, "room_extra": 208, "room_hotel": 204}[seg],
                             "price": 80 + 5 * i}
    world["it/fuori"] = {"lat": 45.70, "lon": 13.80, "type_id": 201, "price": 60}
    world[OWN] = {"lat": 45.6440, "lon": 13.7612, "type_id": 201, "price": 100}
    return world


class FakeScraper:
    world: dict = {}
    sold: set = set()          # (booking_id, checkin) non disponibili
    mode: str = "ok"           # ok | broken_prices | blocked | empty

    def __init__(self, cfg, run_id, known):
        self.known_coords = known

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def resolve(self, url):
        return f"https://www.booking.com/hotel/{OWN}.it.html"

    def search(self, profile, checkin):
        if self.mode == "blocked":
            raise BlockedError("captcha")
        out = SearchOutcome(profile.key, checkin, pages=1, complete=True, stop_reason="test")
        if self.mode == "empty":
            return out
        for pos, (bid, p) in enumerate(self.world.items(), 1):
            if (bid, checkin) in self.sold:
                continue
            price = None if self.mode == "broken_prices" else p["price"] * profile.nights
            out.cards.append(bp.Card(booking_id=bid, name=bid, url=bp.canonical_url(bid), position=pos,
                                     price_shown=price, price_total=price, rating=8.5, review_count=10,
                                     breakfast=False, free_cancellation=True))
        return out

    def detail(self, url):
        bid = bp.booking_id_from_url(url)
        p = self.world[bid]
        return bp.Detail(lat=p["lat"], lon=p["lon"], type_id=p["type_id"], type_label="Struttura")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    cfg = config.load(ROOT / "config", tmp_path)
    cfg.own["booking_id"] = ""
    for p in cfg.profiles.values():
        object.__setattr__(p, "horizon_days", 6)
        object.__setattr__(p, "near_days", 4)
    conn = db.connect(URL, retries=1)
    db.apply_schema(conn)
    conn.execute("DROP TABLE IF EXISTS observations, staging_results, staging_searches, metrics_market, "
                 "notifications, runs, properties, segment_groups CASCADE")
    conn.commit()
    db.apply_schema(conn)
    monkeypatch.setattr(pipeline, "pause", lambda *_: None)
    FakeScraper.world = make_world(cfg)
    FakeScraper.sold = set()
    FakeScraper.mode = "ok"
    yield cfg, conn
    conn.close()


def run_on(cfg, monkeypatch, day):
    monkeypatch.setattr(pipeline, "today_local", lambda _cfg: day)
    return pipeline.run_nightly(cfg, scraper_cls=FakeScraper, do_backup=False)


def test_valid_runs_build_history_and_metrics(env, monkeypatch):
    cfg, conn = env
    d0 = date(2026, 10, 12)
    r1 = run_on(cfg, monkeypatch, d0)
    assert r1["status"] == "valid", r1

    own = conn.execute("SELECT * FROM properties WHERE is_own").fetchone()
    assert own["booking_id"] == OWN and own["segment"] == "apartment"
    out = conn.execute("SELECT * FROM properties WHERE booking_id = 'it/fuori'").fetchone()
    assert out["in_polygon"] is False
    assert conn.execute("SELECT count(*) AS n FROM properties WHERE in_polygon").fetchone()["n"] == 13

    # Seconda notte: due strutture e il BnB vendute per una data.
    target = d0 + timedelta(days=3)
    FakeScraper.sold = {("it/p0", target), ("it/p3", target), (OWN, target)}
    r2 = run_on(cfg, monkeypatch, d0 + timedelta(days=1))
    assert r2["status"] == "valid", r2

    rows = conn.execute(
        """SELECT booking_id, available, price_per_night FROM observations
           WHERE run_id = %s AND profile = 'principale' AND checkin = %s""", (r2["run_id"], target)).fetchall()
    by_id = {r["booking_id"]: r for r in rows}
    assert by_id["it/p0"]["available"] is False and by_id["it/p3"]["available"] is False
    assert by_id[OWN]["available"] is False
    assert by_id["it/p1"]["available"] is True and float(by_id["it/p1"]["price_per_night"]) == 85.0
    assert "it/fuori" not in by_id or by_id["it/fuori"]["available"] is True

    m = conn.execute(
        """SELECT * FROM metrics_market WHERE observed_on = %s AND profile = 'principale'
           AND checkin = %s AND seg_group = 'appartamenti'""", (d0 + timedelta(days=1), target)).fetchone()
    # appartamenti nel centro: p0, p3, p6, p9 -> 2 disponibili su 4
    assert (m["n_active"], m["n_available"]) == (4, 2)
    assert float(m["availability_rate"]) == 0.5
    assert m["own_available"] is False and m["own_ppn"] is None

    other = conn.execute(
        """SELECT * FROM metrics_market WHERE observed_on = %s AND profile = 'principale'
           AND checkin = %s AND seg_group = 'appartamenti'""", (d0 + timedelta(days=1), d0 + timedelta(days=2))
    ).fetchone()
    assert float(other["median_ppn"]) == pytest.approx((80 + 95 + 110 + 125) / 4)
    assert float(other["own_ppn"]) == 100
    assert float(other["price_index"]) == pytest.approx(100 / 102.5, abs=1e-3)
    assert float(other["own_percentile"]) == 0.5

    # Metriche sul raggio calcolate al momento e quota soggiorno minimo.
    near = metrics.market(conn, radius_m=10)
    assert near.empty or (near["n_active"] <= 4).all()
    ms = metrics.min_stay(conn, "principale", "notte_singola")
    assert not ms.empty and (ms["n_long_only"] == 0).all()

    text = summary.build_summary(conn, cfg)
    assert "riepilogo" in text


def test_suspect_run_stays_in_staging(env, monkeypatch):
    cfg, conn = env
    d0 = date(2026, 10, 12)
    assert run_on(cfg, monkeypatch, d0)["status"] == "valid"
    n_obs = conn.execute("SELECT count(*) AS n FROM observations").fetchone()["n"]

    FakeScraper.mode = "broken_prices"
    r = run_on(cfg, monkeypatch, d0 + timedelta(days=1))
    assert r["status"] == "suspect"
    assert conn.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == n_obs
    assert conn.execute("SELECT count(*) AS n FROM staging_results WHERE run_id = %s",
                        (r["run_id"],)).fetchone()["n"] > 0
    assert conn.execute("SELECT count(*) AS n FROM notifications WHERE kind = 'alarm'").fetchone()["n"] == 1

    FakeScraper.mode = "ok"
    FakeScraper.world = {k: v for k, v in list(FakeScraper.world.items())[:3]}
    assert run_on(cfg, monkeypatch, d0 + timedelta(days=2))["status"] == "suspect"  # crollo dei risultati


def test_blocked_run(env, monkeypatch):
    cfg, conn = env
    FakeScraper.mode = "blocked"
    r = run_on(cfg, monkeypatch, date(2026, 10, 12))
    assert r["status"] == "blocked"
    assert conn.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 0


def test_inactive_after_30_days(env, monkeypatch):
    cfg, conn = env
    d0 = date(2026, 10, 12)
    assert run_on(cfg, monkeypatch, d0)["status"] == "valid"
    conn.execute("UPDATE properties SET last_seen = now() - interval '40 days' WHERE booking_id = 'it/p1'")
    conn.commit()
    del FakeScraper.world["it/p1"]
    r = run_on(cfg, monkeypatch, d0 + timedelta(days=1))
    assert r["status"] == "valid"
    assert conn.execute("SELECT active FROM properties WHERE booking_id = 'it/p1'").fetchone()["active"] is False
    n = conn.execute("SELECT count(*) AS n FROM observations WHERE run_id = %s AND booking_id = 'it/p1'",
                     (r["run_id"],)).fetchone()["n"]
    assert n == 0
