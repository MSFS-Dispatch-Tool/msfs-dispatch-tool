import pytest

import generator
import techlog


@pytest.mark.parametrize("metar, storm", [
    ("EGKK 011650Z 24012KT 9999 VCTS SCT030CB 22/16 Q1012", True),
    ("EGKK 011650Z 24012KT 9999 TS SCT030CB 22/16 Q1012", True),
    ("LIRF 011650Z 24012KT 4000 +TSRA BKN020CB 22/18 Q1010", True),
    ("EGKK 011650Z 24012KT 9999 FEW030 22/16 Q1012 TEMPO TSRA", False),   # forecast, not observed
    ("EGKK 011650Z 24012KT CAVOK 22/16 Q1012 NOSIG", False),
    ("", False),
])
def test_thunderstorm_detection(metar, storm):
    assert generator.parse_metar(metar)["thunderstorm"] is storm


def test_metar_fields():
    m = generator.parse_metar("LFPG 011650Z 27015G28KT 3000 -RA BR BKN008 M02/M04 Q1002")
    assert m["wind_kt"] == 15 and m["visibility_m"] == 3000 and m["temp_c"] == -2
    assert {"RA", "BR"} <= m["phenomena"]
    assert generator.parse_metar("EDDM 011650Z 27010MPS CAVOK 10/02 Q1020")["wind_kt"] == 19


def test_trend_weather_is_ignored_for_observed_phenomena():
    assert generator.parse_metar("EGKK 011650Z 24012KT 9999 FEW030 22/16 Q1012 BECMG -RA")["phenomena"] == set()


def test_weather_conditions_for_mel_checks():
    taf_ts = "TAF EGKK 281100Z 2812/2918 24010KT 9999 SCT040 TEMPO 2814/2818 TSRA BKN030CB"
    assert "TS" in techlog.weather_conditions("EGKK 281050Z 24010KT 9999 FEW040 20/10 Q1015", taf_ts, "dep")
    fog = "EGKK 281050Z 00000KT 0300 FG VV001 08/08 Q1025"
    assert "LVP" in techlog.weather_conditions(fog, None, "arr")
    assert "LVP" not in techlog.weather_conditions(fog, None, "dep")   # judged at the destination only
    assert "ICING" in techlog.weather_conditions("EGKK 281050Z 24010KT 9999 OVC020 02/01 Q1015", None, "dep")
    assert techlog.weather_conditions(None, None, "dep") == set()
