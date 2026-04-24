from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sidae_secretary.connectors.weather_kma import KMAWeatherClient

pytestmark = pytest.mark.beta_critical


class _FakeResponse:
    def __init__(self, *, text: str = "", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def get(self, url: str, params=None, timeout: int = 20):
        params = dict(params or {})
        if "nph-dfs_xy_lonlat" in url:
            return _FakeResponse(text="#START7777\n 127.058701,   37.583801,  61, 127\n")
        base_time = str(params.get("base_time") or "")
        if "getUltraSrtNcst" in url:
            assert base_time == "1500"
            return _FakeResponse(
                payload=_payload(
                    [
                        {"baseDate": "20260309", "baseTime": "1500", "category": "PTY", "obsrValue": "0"},
                        {"baseDate": "20260309", "baseTime": "1500", "category": "REH", "obsrValue": "38"},
                        {"baseDate": "20260309", "baseTime": "1500", "category": "RN1", "obsrValue": "강수없음"},
                        {"baseDate": "20260309", "baseTime": "1500", "category": "T1H", "obsrValue": "7.6"},
                        {"baseDate": "20260309", "baseTime": "1500", "category": "WSD", "obsrValue": "2.7"},
                    ]
                )
            )
        if "getUltraSrtFcst" in url:
            assert base_time == "1430"
            return _FakeResponse(
                payload=_payload(
                    [
                        {
                            "baseDate": "20260309",
                            "baseTime": "1430",
                            "fcstDate": "20260309",
                            "fcstTime": "1500",
                            "category": "SKY",
                            "fcstValue": "1",
                        },
                        {
                            "baseDate": "20260309",
                            "baseTime": "1430",
                            "fcstDate": "20260309",
                            "fcstTime": "1500",
                            "category": "PTY",
                            "fcstValue": "0",
                        },
                    ]
                )
            )
        if "getVilageFcst" in url:
            assert base_time in {"1400", "0200"}
            return _FakeResponse(
                payload=_payload(
                    [
                        _fcst("20260309", "0500", "TMP", "2"),
                        _fcst("20260309", "0500", "SKY", "1"),
                        _fcst("20260309", "0500", "PTY", "0"),
                        _fcst("20260309", "0500", "POP", "0"),
                        _fcst("20260309", "0800", "TMP", "5"),
                        _fcst("20260309", "0800", "SKY", "1"),
                        _fcst("20260309", "0800", "PTY", "0"),
                        _fcst("20260309", "0800", "POP", "10"),
                        _fcst("20260309", "1100", "TMP", "8"),
                        _fcst("20260309", "1100", "SKY", "3"),
                        _fcst("20260309", "1100", "PTY", "0"),
                        _fcst("20260309", "1100", "POP", "20"),
                        _fcst("20260309", "1400", "TMP", "12"),
                        _fcst("20260309", "1400", "SKY", "4"),
                        _fcst("20260309", "1400", "PTY", "1"),
                        _fcst("20260309", "1400", "POP", "60"),
                        _fcst("20260309", "1700", "TMP", "13"),
                        _fcst("20260309", "1700", "SKY", "4"),
                        _fcst("20260309", "1700", "PTY", "1"),
                        _fcst("20260309", "1700", "POP", "70"),
                        _fcst("20260309", "0500", "TMN", "2"),
                        _fcst("20260309", "1400", "TMX", "13"),
                        _fcst("20260310", "0500", "TMP", "4"),
                        _fcst("20260310", "0500", "SKY", "1"),
                        _fcst("20260310", "0500", "PTY", "0"),
                        _fcst("20260310", "0500", "POP", "0"),
                        _fcst("20260310", "1400", "TMP", "11"),
                        _fcst("20260310", "1400", "SKY", "3"),
                        _fcst("20260310", "1400", "PTY", "0"),
                        _fcst("20260310", "1400", "POP", "20"),
                        _fcst("20260310", "0500", "TMN", "4"),
                        _fcst("20260310", "1400", "TMX", "11"),
                    ]
                )
            )
        raise AssertionError(f"unexpected URL: {url}")


def _payload(items):
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {"items": {"item": items}},
        }
    }


def _fcst(fcst_date: str, fcst_time: str, category: str, value: str) -> dict[str, str]:
    return {
        "baseDate": "20260309",
        "baseTime": "1400",
        "fcstDate": fcst_date,
        "fcstTime": fcst_time,
        "category": category,
        "fcstValue": value,
        "nx": 61,
        "ny": 127,
    }


def test_kma_weather_client_builds_current_and_dayparts() -> None:
    tz = ZoneInfo("Asia/Seoul")
    client = KMAWeatherClient(auth_key="test-key", session=_FakeSession())

    snapshot = client.fetch_snapshot(
        lat=37.583801,
        lon=127.058701,
        location_label="서울시립대",
        timezone_name="Asia/Seoul",
        now_local=datetime(2026, 3, 9, 15, 10, tzinfo=tz),
    )

    assert snapshot["grid"]["x"] == 61
    assert snapshot["grid"]["y"] == 127
    assert snapshot["current"]["temperature_c"] == 7.6
    assert snapshot["current"]["condition_text"] == "맑음"
    assert snapshot["today"]["morning"]["condition_text"] == "맑음"
    assert snapshot["today"]["afternoon"]["condition_text"] == "비"
    assert snapshot["today"]["afternoon"]["precip_probability_max"] == 70
    assert snapshot["today"]["diurnal_range_c"] == 11.0
    assert snapshot["today"]["diurnal_range_alert"] is True
    assert snapshot["tomorrow"]["temperature_min_c"] == 4.0
    assert snapshot["tomorrow"]["temperature_max_c"] == 11.0
