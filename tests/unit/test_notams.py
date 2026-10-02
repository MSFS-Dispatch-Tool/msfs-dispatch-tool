from datetime import datetime, timezone

import pytest

import notams

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("qcode, text, level", [
    ("QMRLC", "RWY 08R/26L CLSD DUE WIP", "major"),
    ("QICAS", "ILS RWY 26L U/S", "major"),
    ("QFALC", "AD CLSD", "major"),
    ("QFUAU", "JET A1 NOT AVBL", "major"),
    ("QLAAS", "APCH LGT RWY 26L U/S", "significant"),
    ("QMDCH", "RWY 26L TORA 2800M", "significant"),
    ("QNVAS", "VOR MAY U/S", "significant"),
    ("QMXLC", "TWY J CLSD", "other"),
    ("QOBCE", "CRANE ERECTED 1NM FINAL RWY 26L", "other"),
    ("", "RWY 09 CLSD", "major"),               # no Q-code: graded by the text
    ("", "PAPI RWY 27 U/S", "significant"),
    ("", "BIRD CONCENTRATION IN THE VICINITY OF AD", "other"),
])
def test_grading(qcode, text, level):
    assert notams.grade(qcode, text)[0] == level


def test_only_live_notams_and_major_first():
    items = [
        {"notam_id": "A1", "notam_qcode": "QMXLC", "notam_text": "TWY J CLSD", "date_effective": "2026-10-01 00:00:00", "date_expire": "PERM"},
        {"notam_id": "A2", "notam_qcode": "QMRLC", "notam_text": "RWY 26L CLSD", "date_effective": int(NOW.timestamp()) - 60, "date_expire": int(NOW.timestamp()) + 3600},
        {"notam_id": "A3", "notam_qcode": "QICAS", "notam_text": "ILS U/S", "date_effective": "2026-10-05T00:00:00Z"},   # not yet
        {"notam_id": "A4", "notam_qcode": "QICAS", "notam_text": "ILS U/S", "date_expire": "2026-09-30T00:00:00Z"},     # expired
        {"notam_id": "A5", "notam_text": ""},                                                                           # empty
    ]
    out = notams.airport_notams(items, "EGKK", NOW)
    assert [n["id"] for n in out] == ["A2", "A1"]
    assert out[0]["level"] == "major" and out[0]["reason"] == "runway closed"
    assert out[1]["to"] == "PERM"


def test_ofp_shapes():
    one = {"notam_id": "B1", "notam_qcode": "QFALC", "notam_text": "AD CLSD 2200-0600"}
    data = {"origin": {"icao_code": "EGKK", "notam": [one]}, "destination": {"icao_code": "EGAC", "notam": one}}
    out = notams.ofp_notams(data, NOW)
    assert [n["icao"] for n in out] == ["EGKK", "EGAC"]           # a single NOTAM can come as an object
    assert notams.ofp_notams({"origin": {"icao_code": "EGKK"}}) is None   # NOTAMs off in SimBrief
    assert notams.ofp_notams({"origin": {"icao_code": "EGKK", "notam": []}}) == []
    assert notams.ofp_notams({"origin": "junk", "destination": None}) is None
