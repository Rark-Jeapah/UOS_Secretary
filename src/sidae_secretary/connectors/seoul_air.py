from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests


SEOUL_OPENAPI_BASE = "http://openAPI.seoul.go.kr:8088"
SEOUL_OPENAPI_SAMPLE_KEY = "sample"
DEFAULT_DISTRICT_CODES = ("111152", "111171")
DISTRICT_NAMES = {
    "111152": "동대문구",
    "111171": "도봉구",
}


@dataclass
class SeoulAirDistrictReading:
    district_code: str
    district_name: str
    measured_at: str | None
    cai: int | None
    cai_grade: str | None
    dominant_pollutant: str | None
    pm10: int | None
    pm25: int | None
    no2: float | None
    o3: float | None
    co: float | None
    so2: float | None


class SeoulAirQualityClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = SEOUL_OPENAPI_BASE,
        timeout_sec: int = 20,
        session: requests.sessions.Session | None = None,
    ) -> None:
        self.api_key = str(api_key or SEOUL_OPENAPI_SAMPLE_KEY).strip() or SEOUL_OPENAPI_SAMPLE_KEY
        self.base_url = str(base_url or SEOUL_OPENAPI_BASE).rstrip("/")
        self.timeout_sec = max(int(timeout_sec), 1)
        self.session = session or requests.Session()

    def fetch_snapshot(
        self,
        *,
        district_codes: list[str] | tuple[str, ...] | None = None,
        timezone_name: str = "Asia/Seoul",
    ) -> dict[str, Any]:
        selected_codes = [
            str(code).strip()
            for code in (district_codes or DEFAULT_DISTRICT_CODES)
            if str(code).strip()
        ]
        if not selected_codes:
            raise ValueError("at least one Seoul district code is required")

        rows: list[dict[str, Any]] = []
        for district_code in selected_codes:
            rows.extend(self._fetch_rows_for_district(district_code))
        if not rows:
            raise RuntimeError("no Seoul air quality rows returned")

        parsed_rows: list[dict[str, Any]] = []
        measured_candidates: list[str] = []
        for row in rows:
            reading = _parse_district_reading(row=row, timezone_name=timezone_name)
            parsed = {
                "district_code": reading.district_code,
                "district_name": reading.district_name,
                "measured_at": reading.measured_at,
                "cai": reading.cai,
                "cai_grade": reading.cai_grade,
                "dominant_pollutant": reading.dominant_pollutant,
                "pm10": reading.pm10,
                "pm25": reading.pm25,
                "no2": reading.no2,
                "o3": reading.o3,
                "co": reading.co,
                "so2": reading.so2,
            }
            if reading.measured_at:
                measured_candidates.append(reading.measured_at)
            parsed_rows.append(parsed)

        measured_at = max(measured_candidates) if measured_candidates else None
        return {
            "provider": "seoul_openapi",
            "measured_at": measured_at,
            "districts": parsed_rows,
        }

    def _fetch_rows_for_district(self, district_code: str) -> list[dict[str, Any]]:
        url = (
            f"{self.base_url}/{self.api_key}/json/"
            f"ListAirQualityByDistrictService/1/5/{district_code}/"
        )
        response = self.session.get(url, timeout=self.timeout_sec)
        response.raise_for_status()
        payload = response.json()
        service = (
            payload.get("ListAirQualityByDistrictService", {})
            if isinstance(payload, dict)
            else {}
        )
        result = service.get("RESULT", {}) if isinstance(service, dict) else {}
        result_code = str(result.get("CODE") or "").strip()
        if result_code not in {"INFO-000", "INFO-200"}:
            raise RuntimeError(str(result.get("MESSAGE") or "Seoul air API error"))
        rows = service.get("row", []) if isinstance(service, dict) else []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]


def _parse_district_reading(
    *,
    row: dict[str, Any],
    timezone_name: str,
) -> SeoulAirDistrictReading:
    district_code = str(row.get("MSRSTN_PBADMS_CD") or "").strip()
    district_name = str(row.get("MSRSTN_NM") or DISTRICT_NAMES.get(district_code) or district_code).strip()
    return SeoulAirDistrictReading(
        district_code=district_code,
        district_name=district_name,
        measured_at=_parse_measurement_time(row.get("MSRMT_YMD"), timezone_name),
        cai=_to_int(row.get("CAI")),
        cai_grade=str(row.get("CAI_GRD") or "").strip() or None,
        dominant_pollutant=str(row.get("CRST_SBSTN") or "").strip() or None,
        pm10=_to_int(row.get("PM")),
        pm25=_to_int(row.get("FPM")),
        no2=_to_float(row.get("NTDX")),
        o3=_to_float(row.get("OZON")),
        co=_to_float(row.get("CBMX")),
        so2=_to_float(row.get("SPDX")),
    )


def _parse_measurement_time(value: Any, timezone_name: str) -> str | None:
    text = str(value or "").strip()
    if len(text) != 12:
        return None
    try:
        dt = datetime(
            int(text[:4]),
            int(text[4:6]),
            int(text[6:8]),
            int(text[8:10]),
            int(text[10:12]),
            tzinfo=ZoneInfo(timezone_name),
        )
    except ValueError:
        return None
    return dt.isoformat()


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None
