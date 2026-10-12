from datetime import date, timedelta

import pytest

from bnbmonitor import config, geo, schedule
from bnbmonitor.config import Profile
from conftest import ROOT


@pytest.fixture(scope="module")
def cfg():
    return config.load(ROOT / "config")


def test_polygon_contains_centroid_and_not_far_points(cfg):
    lat, lon = cfg.search_point
    assert geo.point_in_polygon(lat, lon, cfg.polygon)
    assert not geo.point_in_polygon(45.70, 13.80, cfg.polygon)  # fuori Trieste centro
    assert not geo.point_in_polygon(lat, lon + 0.05, cfg.polygon)


def test_square_polygon():
    square = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert geo.point_in_polygon(0.5, 0.5, square)
    assert not geo.point_in_polygon(1.5, 0.5, square)
    assert geo.centroid(square) == pytest.approx((0.5, 0.5))


def test_haversine_known_distance():
    # Piazza Unità -> Stazione centrale di Trieste: circa 750 m.
    d = geo.haversine_m(45.6503, 13.7676, 45.6574, 13.7716)
    assert 700 < d < 900


def test_stop_radius_covers_polygon(cfg):
    assert cfg.stop_radius_m > geo.max_vertex_distance(cfg.search_point, cfg.polygon)


def test_circle_radius():
    pts = geo.circle(45.65, 13.77, 500, n=16)
    for lat, lon in pts:
        assert geo.haversine_m(45.65, 13.77, lat, lon) == pytest.approx(500, rel=0.01)


PROFILE = Profile("principale", "2 notti", 2, 2, 90, 30, 3)


def test_dates_near_every_night_far_rotating():
    today = date(2026, 10, 12)
    dates = schedule.dates_to_check(PROFILE, today)
    assert dates[:30] == [today + timedelta(days=i) for i in range(30)]
    far = [d for d in dates if (d - today).days >= 30]
    assert 19 <= len(far) <= 21
    assert max(dates) <= today + timedelta(days=89)


def test_each_far_date_checked_every_three_nights():
    target = date(2027, 1, 1)
    checks = [date(2026, 10, 12) + timedelta(days=k) for k in range(30)]
    hit = [t for t in checks if target in schedule.dates_to_check(PROFILE, t) and (target - t).days >= 30]
    gaps = {(b - a).days for a, b in zip(hit, hit[1:])}
    assert gaps == {3}


def test_profiles_share_far_dates():
    single = Profile("notte_singola", "1 notte", 1, 2, 60, 30, 3)
    today = date(2026, 10, 12)
    a = set(schedule.dates_to_check(PROFILE, today))
    b = set(schedule.dates_to_check(single, today))
    assert b <= a
