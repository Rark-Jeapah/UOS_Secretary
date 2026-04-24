from __future__ import annotations

from sidae_secretary.connectors.seoul_air import SeoulAirQualityClient


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def get(self, url: str, timeout: int = 20):
        if url.endswith("/111152/"):
            return _FakeResponse(
                {
                    "ListAirQualityByDistrictService": {
                        "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                        "row": [
                            {
                                "MSRMT_YMD": "202603091500",
                                "MSRSTN_PBADMS_CD": "111152",
                                "MSRSTN_NM": "동대문구",
                                "CAI": "95",
                                "CAI_GRD": "보통",
                                "CRST_SBSTN": "PM-2.5",
                                "NTDX": "0.008",
                                "OZON": "0.046",
                                "CBMX": "0.4",
                                "SPDX": "0.003",
                                "PM": "56",
                                "FPM": "37",
                            }
                        ],
                    }
                }
            )
        if url.endswith("/111171/"):
            return _FakeResponse(
                {
                    "ListAirQualityByDistrictService": {
                        "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                        "row": [
                            {
                                "MSRMT_YMD": "202603091500",
                                "MSRSTN_PBADMS_CD": "111171",
                                "MSRSTN_NM": "도봉구",
                                "CAI": "95",
                                "CAI_GRD": "보통",
                                "CRST_SBSTN": "PM-2.5",
                                "NTDX": "0.008",
                                "OZON": "0.057",
                                "CBMX": "0.5",
                                "SPDX": "0.003",
                                "PM": "58",
                                "FPM": "36",
                            }
                        ],
                    }
                }
            )
        raise AssertionError(f"unexpected URL {url}")


def test_seoul_air_quality_client_fetches_two_districts() -> None:
    client = SeoulAirQualityClient(api_key="sample", session=_FakeSession())

    snapshot = client.fetch_snapshot(
        district_codes=["111152", "111171"],
        timezone_name="Asia/Seoul",
    )

    assert snapshot["provider"] == "seoul_openapi"
    assert snapshot["measured_at"] == "2026-03-09T15:00:00+09:00"
    assert [row["district_name"] for row in snapshot["districts"]] == ["동대문구", "도봉구"]
    assert snapshot["districts"][0]["pm10"] == 56
    assert snapshot["districts"][0]["pm25"] == 37
    assert snapshot["districts"][1]["dominant_pollutant"] == "PM-2.5"
