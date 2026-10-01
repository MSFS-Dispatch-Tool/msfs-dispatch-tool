import random

import techlog

RADAR = {"id": "mel-x1", "system": "Weather radar", "ata": "34", "weight": 1,
         "effects": {"no_wx": ["TS"], "extra_fuel_min": 5}}
PACK = {"id": "mel-x2", "system": "Air conditioning pack", "ata": "21", "weight": 1,
        "effects": {"max_fl": 250, "extra_fuel_min": 10}}
PUMP = {"id": "mel-x3", "system": "Main tank fuel pump", "ata": "28", "weight": 1,
        "effects": {"min_main_tank_fuel_kg": {"takeoff": 3402, "landing": 1000}, "ground_support": ["GPU"]}}
PANEL = {"id": "cdl-x1", "part": "Flap fairing", "ata": "57", "weight": 1,
         "effects": {"fuel_burn_pct": 1.0, "weight_penalty_kg": 50}}
CALM = {"metar": "EGKK 281050Z 24010KT CAVOK 22/10 Q1015", "taf": "TAF EGKK 281100Z 2812/2918 24010KT CAVOK"}
STORMY = {"metar": "LEPA 281050Z 24010KT 9999 FEW040CB 25/18 Q1010",
          "taf": "TAF LEPA 281100Z 2812/2918 24010KT 9999 TEMPO 2814/2818 TSRA"}


def test_effects_add_up_for_simbrief():
    tech = techlog.leg_tech({"mels": [RADAR, PACK], "cdl": [PANEL]}, "EGKK", "LEPA", CALM, CALM, 120)
    assert tech["max_fl"] == 250
    assert tech["extra_fuel_min"] == 5 + 10 + 2          # 1% of 120 min, rounded up
    assert tech["weight_penalty_kg"] == 50
    assert not tech["nogo"]
    assert "MEL 34 WEATHER RADAR" in tech["remarks"] and "MAX FL250" in tech["remarks"]


def test_weather_radar_is_nogo_into_thunderstorms():
    tech = techlog.leg_tech({"mels": [RADAR], "cdl": []}, "EGKK", "LEPA", CALM, STORMY, 120)
    assert tech["nogo"]
    assert techlog.nogo_items([tech]) == {"mel-x1"}


def test_missing_weather_is_a_caution_not_a_pass():
    tech = techlog.leg_tech({"mels": [RADAR], "cdl": []}, "EGKK", "LEPA", {}, {}, 120)
    assert not tech["nogo"] and any(c["level"] == "caution" for c in tech["checks"])


def test_fuel_minimums_and_ground_support():
    tech = techlog.leg_tech({"mels": [PUMP], "cdl": []}, "EGKK", "LEPA", CALM, CALM, 120)
    assert tech["min_takeoff_fuel_kg"] == 6804 and tech["min_landing_fuel_kg"] == 2000
    assert tech["ground_support"] == ["GPU"]


def test_pilot_probability_for_items():
    rng = random.Random(5)
    mels, cdls = [RADAR, PACK, PUMP], [PANEL]
    never = [techlog.roll_tech_status(mels, cdls, {"probability": 0}, {"probability": 0}, rng=rng) for _ in range(200)]
    assert all(not s["mels"] and not s["cdl"] for s in never)
    always = [techlog.roll_tech_status(mels, cdls, {"probability": 100}, {"probability": 100}, rng=rng) for _ in range(200)]
    assert all(s["mels"] and s["cdl"] for s in always)


def test_excluded_and_disabled_items_never_roll():
    rng = random.Random(9)
    for _ in range(200):
        s = techlog.roll_tech_status([RADAR, PACK, PUMP], [], {"probability": 100, "disabled_ids": ["mel-x2"]},
                                     exclude_ids={"mel-x1"}, rng=rng)
        assert {m["id"] for m in s["mels"]} <= {"mel-x3"}
