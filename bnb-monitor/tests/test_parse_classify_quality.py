from datetime import date

import pytest

from bnbmonitor import booking_parse as bp, classify, quality
from conftest import FIXTURES

RULES = {
    "type_ids": {201: "apartment", 204: "room_hotel", 208: "room_extra"},
    "keywords": [["aparthotel", "room_hotel"], ["b&b", "room_extra"], ["hotel", "room_hotel"],
                 ["appartament", "apartment"]],
    "overrides": {"it/speciale": "other"},
}


@pytest.mark.parametrize("text,value", [
    ("€ 1.240", 1240.0), ("€ 98,50", 98.5), ("EUR 1,234.56", 1234.56), ("€ 85", 85.0),
    ("+€ 18 tasse", 18.0), ("", None), ("prezzo", None),
])
def test_parse_amount(text, value):
    assert bp.parse_amount(text) == value


def test_parse_rating_and_distance():
    assert bp.parse_rating("Punteggio di 8,9 8,9 Favoloso 1.234 recensioni") == (8.9, 1234)
    assert bp.parse_rating("10 Eccezionale 3 recensioni") == (10.0, 3)
    assert bp.parse_distance_m("a 350 m dal centro") == 350
    assert bp.parse_distance_m("1,2 km dal centro") == 1200


def test_parse_search_page():
    cards = bp.parse_search_page((FIXTURES / "search_page.html").read_text(), include_taxes=True)
    assert [c.booking_id for c in cards] == ["it/casa-sul-molo", "it/hotel-centrale-trieste", "it/bb-cavana"]
    molo, hotel, cavana = cards
    assert molo.price_shown == 1240 and molo.taxes_extra == 18 and molo.price_total == 1258
    assert molo.rating == 8.9 and molo.review_count == 1234
    assert molo.free_cancellation and not molo.breakfast
    assert molo.distance_text_m == 350
    assert hotel.price_total == 210 and hotel.taxes_included and hotel.breakfast
    assert cavana.price_total is None and cavana.sold_out
    assert [c.position for c in cards] == [1, 2, 3]


def test_parse_search_page_without_taxes():
    cards = bp.parse_search_page((FIXTURES / "search_page.html").read_text(), include_taxes=False)
    assert cards[0].price_total == 1240


def test_parse_detail_page():
    d = bp.parse_detail_page((FIXTURES / "detail_page.html").read_text())
    assert (d.lat, d.lon) == (45.65012, 13.76598)
    assert d.type_id == 201
    assert d.rating == 8.9 and d.review_count == 1234
    assert "Hotel" in d.type_label


def test_booking_id_and_urls():
    assert bp.booking_id_from_url("https://www.booking.com/hotel/it/abc-def.it.html?x=1") == "it/abc-def"
    assert bp.booking_id_from_url("https://www.booking.com/Share-f5lhGq") is None
    assert bp.canonical_url("it/abc") == "https://www.booking.com/hotel/it/abc.it.html"
    url = bp.search_url("https://www.booking.com/searchresults.it.html", {"order": "distance_from_search"},
                        45.65, 13.77, date(2026, 11, 6), date(2026, 11, 8), 2, offset=25)
    assert "checkin=2026-11-06" in url and "checkout=2026-11-08" in url and "offset=25" in url
    assert "group_adults=2" in url and "order=distance_from_search" in url


def test_block_detection():
    assert bp.is_blocked("https://www.booking.com/x", "<div id='px-captcha'></div>")
    assert bp.is_blocked("https://www.booking.com/x", "", status=429)
    assert not bp.is_blocked("https://www.booking.com/searchresults.it.html", "<div data-testid='property-card'>")


def test_classify_order():
    assert classify.segment_for("it/speciale", 201, rules=RULES) == "other"
    assert classify.segment_for("it/a", 201, "Hotel", rules=RULES) == "apartment"
    assert classify.segment_for("it/a", None, "Aparthotel Mare", rules=RULES) == "room_hotel"
    assert classify.segment_for("it/a", None, None, "Appartamento con 1 camera", rules=RULES) == "apartment"
    assert classify.segment_for("it/a", None, "B&B Cavana", rules=RULES) == "room_extra"
    assert classify.segment_for("it/a", 999, "Struttura", rules=RULES) is None


QCFG = {"min_price_read_ratio": 0.8, "max_incomplete_ratio": 0.2, "drop_threshold": 0.5}


def stats(**kw):
    base = dict(n_searches=10, n_searches_complete=10, n_cards=400, n_cards_priced=380, n_cards_sold_out=10)
    base.update(kw)
    return quality.RunStats(**base)


def test_quality_valid():
    v = quality.evaluate(stats(), [40, 42, 38], QCFG)
    assert v.status == "valid", v.reasons


def test_quality_drop_and_unreadable_prices():
    assert quality.evaluate(stats(n_cards=150, n_cards_priced=140), [40, 42], QCFG).status == "suspect"
    assert quality.evaluate(stats(n_cards_priced=100), [], QCFG).status == "suspect"
    assert quality.evaluate(stats(n_searches_complete=6), [], QCFG).status == "suspect"


def test_quality_failed_and_blocked():
    assert quality.evaluate(stats(blocked=True), [], QCFG).status == "blocked"
    assert quality.evaluate(stats(n_searches_complete=0, error="boom"), [], QCFG).status == "failed"
    assert quality.evaluate(stats(n_searches=0, n_searches_complete=0), [], QCFG).status == "failed"
