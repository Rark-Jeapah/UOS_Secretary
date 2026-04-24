from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

import requests


KMA_API_BASE = "https://apihub.kma.go.kr/api"
KMA_SAMPLE_AUTH_KEY = "4_Juann2Raiybmp59uWoBQ"
DEFAULT_TIMEOUT_SEC = 20
DEFAULT_GEOCODER_TIMEOUT_SEC = 10
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "sidae-secretary/1.0"
VILLAGE_FORECAST_BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
MORNING_START_HOUR = 5
MORNING_END_HOUR = 11
AFTERNOON_START_HOUR = 12
AFTERNOON_END_HOUR = 17

_GRID_COORD_RE = re.compile(
    r"^\s*(?P<lon>-?\d+(?:\.\d+)?)\s*,\s*(?P<lat>-?\d+(?:\.\d+)?)\s*,\s*(?P<x>\d+)\s*,\s*(?P<y>\d+)\s*$"
)
_WEATHER_LOCATION_COORD_RE = re.compile(
    r"^\s*(?P<lat>-?\d+(?:\.\d+)?)\s*,\s*(?P<lon>-?\d+(?:\.\d+)?)\s+(?P<label>.+?)\s*$"
)
_WEATHER_LOCATION_TOKEN_RE = re.compile(r"[^0-9A-Za-z\u3131-\u318E\uAC00-\uD7A3]+")


@dataclass(frozen=True)
class WeatherLocation:
    label: str
    lat: float
    lon: float
    air_quality_district_code: str | None = None
    source: str = "catalog"

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "lat": self.lat,
            "lon": self.lon,
            "air_quality_district_code": self.air_quality_district_code,
            "source": self.source,
        }


@dataclass
class KMAFetchResult:
    base_at_local: datetime
    items: list[dict[str, Any]]


COMMON_WEATHER_LOCATIONS = (
    WeatherLocation(
        label="서울특별시",
        lat=37.5665,
        lon=126.9780,
        source="catalog",
    ),
    WeatherLocation(
        label="서울시립대학교",
        lat=37.583801,
        lon=127.058701,
        air_quality_district_code="111152",
        source="catalog",
    ),
    WeatherLocation(
        label="동대문구",
        lat=37.5744,
        lon=127.0396,
        air_quality_district_code="111152",
        source="catalog",
    ),
    WeatherLocation(
        label="도봉구",
        lat=37.6688,
        lon=127.0471,
        air_quality_district_code="111171",
        source="catalog",
    ),
)


def _normalize_weather_location_key(value: str) -> str:
    normalized = _WEATHER_LOCATION_TOKEN_RE.sub("", str(value or "").strip().lower())
    if normalized.endswith("구"):
        return normalized[:-1]
    return normalized


def _common_weather_location_aliases(location: WeatherLocation) -> set[str]:
    aliases = {
        location.label,
        location.label.replace("특별시", ""),
        location.label.replace("대학교", ""),
    }
    if location.label == "서울시립대학교":
        aliases.update({"서울시립대", "시립대", "uos"})
    expanded: set[str] = set()
    for item in aliases:
        text = str(item or "").strip()
        if not text:
            continue
        expanded.add(text)
        expanded.add(text.replace("서울특별시", "서울"))
        if text.startswith("서울"):
            expanded.add(text[len("서울") :])
        if text.endswith("구"):
            expanded.add(text[:-1])
    return {
        key
        for key in (_normalize_weather_location_key(item) for item in expanded)
        if key
    }


def _resolve_common_weather_location(query: str) -> WeatherLocation | None:
    normalized = _normalize_weather_location_key(query)
    if not normalized:
        return None
    for item in COMMON_WEATHER_LOCATIONS:
        aliases = _common_weather_location_aliases(item)
        if normalized in aliases:
            return item
    return None


def _parse_coordinate_weather_location(query: str) -> WeatherLocation | None:
    match = _WEATHER_LOCATION_COORD_RE.match(str(query or ""))
    if not match:
        return None
    label = str(match.group("label") or "").strip()
    if not label:
        return None
    try:
        lat = float(match.group("lat"))
        lon = float(match.group("lon"))
    except ValueError:
        return None
    return WeatherLocation(
        label=label,
        lat=lat,
        lon=lon,
        source="manual_coordinates",
    )


def _geocode_weather_location(
    query: str,
    *,
    session: requests.sessions.Session | None = None,
    timeout_sec: int = DEFAULT_GEOCODER_TIMEOUT_SEC,
) -> WeatherLocation | None:
    http = session or requests.Session()
    response = http.get(
        NOMINATIM_SEARCH_URL,
        params={
            "q": str(query or "").strip(),
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "kr",
        },
        headers={
            "User-Agent": NOMINATIM_USER_AGENT,
            "Accept-Language": "ko",
        },
        timeout=max(int(timeout_sec), 1),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0] if isinstance(payload[0], dict) else {}
    try:
        lat = float(first.get("lat"))
        lon = float(first.get("lon"))
    except (TypeError, ValueError):
        return None
    raw_label = str(first.get("name") or "").strip()
    if not raw_label:
        display_name = str(first.get("display_name") or "").strip()
        raw_label = display_name.split(",", 1)[0].strip() if display_name else ""
    label = raw_label or str(query or "").strip()
    if not label:
        return None
    return WeatherLocation(
        label=label,
        lat=lat,
        lon=lon,
        source="geocoder",
    )


def resolve_weather_location_query(
    query: str,
    *,
    session: requests.sessions.Session | None = None,
    timeout_sec: int = DEFAULT_GEOCODER_TIMEOUT_SEC,
) -> dict[str, Any] | None:
    parsed = _parse_coordinate_weather_location(query)
    if parsed is not None:
        return parsed.to_payload()
    common = _resolve_common_weather_location(query)
    if common is not None:
        return common.to_payload()
    geocoded = _geocode_weather_location(
        query,
        session=session,
        timeout_sec=timeout_sec,
    )
    return geocoded.to_payload() if geocoded is not None else None


class KMAWeatherClient:
    def __init__(
        self,
        auth_key: str | None = None,
        *,
        api_base: str = KMA_API_BASE,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        session: requests.sessions.Session | None = None,
    ) -> None:
        self.auth_key = str(auth_key or KMA_SAMPLE_AUTH_KEY).strip() or KMA_SAMPLE_AUTH_KEY
        self.api_base = str(api_base or KMA_API_BASE).rstrip("/")
        self.timeout_sec = max(int(timeout_sec), 1)
        self.session = session or requests.Session()
        self._grid_cache: dict[str, dict[str, float | int]] = {}

    def resolve_grid(self, *, lat: float, lon: float) -> dict[str, float | int]:
        cache_key = f"{float(lat):.6f}:{float(lon):.6f}"
        if cache_key in self._grid_cache:
            return dict(self._grid_cache[cache_key])
        response = self.session.get(
            f"{self.api_base}/typ01/cgi-bin/url/nph-dfs_xy_lonlat",
            params={
                "lat": f"{float(lat):.6f}",
                "lon": f"{float(lon):.6f}",
                "help": "0",
                "authKey": self.auth_key,
            },
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        text = str(response.text or "")
        for line in reversed(text.splitlines()):
            match = _GRID_COORD_RE.match(line)
            if not match:
                continue
            resolved = {
                "lat": float(match.group("lat")),
                "lon": float(match.group("lon")),
                "x": int(match.group("x")),
                "y": int(match.group("y")),
            }
            self._grid_cache[cache_key] = dict(resolved)
            return resolved
        raise RuntimeError("failed to resolve KMA DFS grid coordinates")

    def fetch_snapshot(
        self,
        *,
        lat: float,
        lon: float,
        location_label: str,
        timezone_name: str = "Asia/Seoul",
        now_local: datetime | None = None,
    ) -> dict[str, Any]:
        tz = ZoneInfo(timezone_name)
        anchor_local = now_local.astimezone(tz) if now_local is not None else datetime.now(tz)
        grid = self.resolve_grid(lat=float(lat), lon=float(lon))
        nx = int(grid["x"])
        ny = int(grid["y"])
        observation = self._fetch_ultra_observation(
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            now_local=anchor_local,
        )
        ultra_forecast = self._fetch_ultra_forecast(
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            now_local=anchor_local,
        )
        village_forecast = self._fetch_village_forecast(
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            now_local=anchor_local,
        )
        village_coverage = self._fetch_village_coverage_forecast(
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            now_local=anchor_local,
        )
        merged_village_items = _merge_forecast_items(
            base_items=village_coverage.items,
            override_items=village_forecast.items,
        )

        current = _build_current_weather(
            observation=observation.items,
            ultra_forecast=ultra_forecast.items,
            village_forecast=merged_village_items,
            timezone_name=timezone_name,
            now_local=anchor_local,
        )
        daily = _build_daily_forecasts(
            village_forecast=merged_village_items,
            timezone_name=timezone_name,
        )
        today_key = anchor_local.date().isoformat()
        tomorrow_key = (anchor_local.date() + timedelta(days=1)).isoformat()
        today = daily.get(today_key)
        tomorrow = daily.get(tomorrow_key)

        return {
            "generated_at": anchor_local.isoformat(),
            "location_label": str(location_label or "").strip() or "기준 위치",
            "grid": {
                "x": nx,
                "y": ny,
                "lat": float(grid["lat"]),
                "lon": float(grid["lon"]),
            },
            "observed_at": current.get("observed_at"),
            "current": current,
            "today": today,
            "tomorrow": tomorrow,
            "source": {
                "provider": "kma_apihub",
                "observation_base_at": observation.base_at_local.isoformat(),
                "ultra_forecast_base_at": ultra_forecast.base_at_local.isoformat(),
                "village_forecast_base_at": village_forecast.base_at_local.isoformat(),
                "village_coverage_base_at": village_coverage.base_at_local.isoformat(),
            },
        }

    def _fetch_ultra_observation(
        self,
        *,
        nx: int,
        ny: int,
        timezone_name: str,
        now_local: datetime,
    ) -> KMAFetchResult:
        candidates = []
        cursor = now_local.replace(minute=0, second=0, microsecond=0)
        for offset in range(0, 6):
            candidates.append(cursor - timedelta(hours=offset))
        return self._fetch_first_available(
            path="/typ02/openApi/VilageFcstInfoService_2.0/getUltraSrtNcst",
            candidates=candidates,
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            base_minute=0,
        )

    def _fetch_ultra_forecast(
        self,
        *,
        nx: int,
        ny: int,
        timezone_name: str,
        now_local: datetime,
    ) -> KMAFetchResult:
        base_cursor = now_local.replace(second=0, microsecond=0)
        if base_cursor.minute < 30:
            base_cursor = (base_cursor - timedelta(hours=1)).replace(minute=30)
        else:
            base_cursor = base_cursor.replace(minute=30)
        candidates = [base_cursor - timedelta(hours=offset) for offset in range(0, 6)]
        return self._fetch_first_available(
            path="/typ02/openApi/VilageFcstInfoService_2.0/getUltraSrtFcst",
            candidates=candidates,
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            base_minute=30,
        )

    def _fetch_village_forecast(
        self,
        *,
        nx: int,
        ny: int,
        timezone_name: str,
        now_local: datetime,
    ) -> KMAFetchResult:
        candidates: list[datetime] = []
        cursor_date = now_local.date()
        for day_offset in range(0, 3):
            target_date = cursor_date - timedelta(days=day_offset)
            for hour in reversed(VILLAGE_FORECAST_BASE_HOURS):
                candidate = datetime(
                    target_date.year,
                    target_date.month,
                    target_date.day,
                    hour,
                    0,
                    tzinfo=ZoneInfo(timezone_name),
                )
                if candidate <= now_local:
                    candidates.append(candidate)
        return self._fetch_first_available(
            path="/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst",
            candidates=candidates,
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            base_minute=0,
        )

    def _fetch_village_coverage_forecast(
        self,
        *,
        nx: int,
        ny: int,
        timezone_name: str,
        now_local: datetime,
    ) -> KMAFetchResult:
        tz = ZoneInfo(timezone_name)
        if now_local.hour >= 2:
            coverage_anchor = datetime(
                now_local.year,
                now_local.month,
                now_local.day,
                2,
                0,
                tzinfo=tz,
            )
            candidates = [
                coverage_anchor,
                coverage_anchor - timedelta(hours=3),
            ]
        else:
            previous_day = now_local.date() - timedelta(days=1)
            candidates = [
                datetime(previous_day.year, previous_day.month, previous_day.day, 23, 0, tzinfo=tz),
                datetime(previous_day.year, previous_day.month, previous_day.day, 20, 0, tzinfo=tz),
            ]
        return self._fetch_first_available(
            path="/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst",
            candidates=candidates,
            nx=nx,
            ny=ny,
            timezone_name=timezone_name,
            base_minute=0,
        )

    def _fetch_first_available(
        self,
        *,
        path: str,
        candidates: list[datetime],
        nx: int,
        ny: int,
        timezone_name: str,
        base_minute: int,
    ) -> KMAFetchResult:
        errors: list[str] = []
        tz = ZoneInfo(timezone_name)
        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.astimezone(tz).replace(minute=base_minute, second=0, microsecond=0)
            key = normalized.isoformat()
            if key in seen:
                continue
            seen.add(key)
            try:
                items = self._request_json_items(
                    path=path,
                    base_date=normalized.strftime("%Y%m%d"),
                    base_time=normalized.strftime("%H%M"),
                    nx=nx,
                    ny=ny,
                )
            except Exception as exc:
                errors.append(f"{normalized.isoformat()}: {exc}")
                continue
            if items:
                return KMAFetchResult(base_at_local=normalized, items=items)
        raise RuntimeError("; ".join(errors) or "no KMA forecast data available")

    def _request_json_items(
        self,
        *,
        path: str,
        base_date: str,
        base_time: str,
        nx: int,
        ny: int,
    ) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.api_base}{path}",
            params={
                "pageNo": 1,
                "numOfRows": 1200,
                "dataType": "JSON",
                "base_date": str(base_date),
                "base_time": str(base_time),
                "nx": int(nx),
                "ny": int(ny),
                "authKey": self.auth_key,
            },
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        header = (
            payload.get("response", {}).get("header", {})
            if isinstance(payload, dict)
            else {}
        )
        if str(header.get("resultCode") or "") != "00":
            raise RuntimeError(str(header.get("resultMsg") or "KMA API error"))
        items = (
            payload.get("response", {})
            .get("body", {})
            .get("items", {})
            .get("item", [])
        )
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]


def _build_current_weather(
    *,
    observation: list[dict[str, Any]],
    ultra_forecast: list[dict[str, Any]],
    village_forecast: list[dict[str, Any]],
    timezone_name: str,
    now_local: datetime,
) -> dict[str, Any]:
    obs_map: dict[str, Any] = {}
    observed_at: str | None = None
    for item in observation:
        category = str(item.get("category") or "").strip().upper()
        if category:
            obs_map[category] = item.get("obsrValue")
        if observed_at is None:
            base_date = str(item.get("baseDate") or "").strip()
            base_time = str(item.get("baseTime") or "").strip()
            observed_at = _combine_kma_datetime(base_date, base_time, timezone_name)
    ultra_slots = _group_forecast_slots(ultra_forecast, timezone_name=timezone_name)
    village_slots = _group_forecast_slots(village_forecast, timezone_name=timezone_name)
    closest_slot = _pick_closest_slot(now_local=now_local, slots=ultra_slots) or _pick_closest_slot(
        now_local=now_local,
        slots=village_slots,
    )
    sky_code = closest_slot.get("SKY") if isinstance(closest_slot, dict) else None
    pty_code = obs_map.get("PTY")
    condition_text = _condition_text(sky_code=sky_code, pty_code=pty_code)
    return {
        "observed_at": observed_at,
        "temperature_c": _to_float(obs_map.get("T1H")),
        "humidity_pct": _to_int(obs_map.get("REH")),
        "wind_speed_mps": _to_float(obs_map.get("WSD")),
        "precip_1h_mm": _to_precip_mm(obs_map.get("RN1")),
        "precip_text": _precip_amount_text(obs_map.get("RN1")),
        "condition_text": condition_text,
        "precip_type_text": _precip_type_text(pty_code),
        "sky_text": _sky_text(sky_code),
    }


def _build_daily_forecasts(
    *,
    village_forecast: list[dict[str, Any]],
    timezone_name: str,
) -> dict[str, dict[str, Any]]:
    slots = _group_forecast_slots(village_forecast, timezone_name=timezone_name)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for slot in slots:
        fcst_at = _parse_dt(slot.get("fcst_at"))
        if fcst_at is None:
            continue
        grouped.setdefault(fcst_at.date().isoformat(), []).append(slot)

    output: dict[str, dict[str, Any]] = {}
    for date_key, date_slots in grouped.items():
        morning_slots = [
            slot for slot in date_slots if MORNING_START_HOUR <= int(slot.get("hour") or 0) <= MORNING_END_HOUR
        ]
        afternoon_slots = [
            slot
            for slot in date_slots
            if AFTERNOON_START_HOUR <= int(slot.get("hour") or 0) <= AFTERNOON_END_HOUR
        ]
        fallback_slots = sorted(date_slots, key=lambda item: int(item.get("hour") or 0))
        tmin = _extract_daily_extreme(date_slots, category="TMN")
        tmax = _extract_daily_extreme(date_slots, category="TMX")
        derived_min = _min_float(slot.get("TMP") for slot in date_slots)
        derived_max = _max_float(slot.get("TMP") for slot in date_slots)
        temp_min = tmin if tmin is not None else derived_min
        temp_max = tmax if tmax is not None else derived_max
        diurnal_range = None
        if temp_min is not None and temp_max is not None:
            diurnal_range = round(float(temp_max) - float(temp_min), 1)
        output[date_key] = {
            "date": date_key,
            "temperature_min_c": temp_min,
            "temperature_max_c": temp_max,
            "diurnal_range_c": diurnal_range,
            "diurnal_range_alert": bool(diurnal_range is not None and diurnal_range >= 10.0),
            "morning": _summarize_daypart("오전", morning_slots or fallback_slots[:3]),
            "afternoon": _summarize_daypart("오후", afternoon_slots or fallback_slots[-3:]),
        }
    return output


def _group_forecast_slots(
    items: list[dict[str, Any]],
    *,
    timezone_name: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        fcst_date = str(item.get("fcstDate") or "").strip()
        fcst_time = str(item.get("fcstTime") or "").strip()
        category = str(item.get("category") or "").strip().upper()
        if not fcst_date or not fcst_time or not category:
            continue
        key = f"{fcst_date}{fcst_time}"
        slot = grouped.setdefault(
            key,
            {
                "fcst_at": _combine_kma_datetime(fcst_date, fcst_time, timezone_name),
                "hour": int(fcst_time[:2]),
            },
        )
        slot[category] = item.get("fcstValue")
    return sorted(grouped.values(), key=lambda item: str(item.get("fcst_at") or ""))


def _merge_forecast_items(
    *,
    base_items: list[dict[str, Any]],
    override_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in list(base_items) + list(override_items):
        key = "|".join(
            [
                str(item.get("fcstDate") or ""),
                str(item.get("fcstTime") or ""),
                str(item.get("category") or ""),
            ]
        )
        if not key.strip(" |"):
            continue
        merged[key] = item
    return list(merged.values())


def _pick_closest_slot(
    *,
    now_local: datetime,
    slots: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not slots:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for slot in slots:
        fcst_at = _parse_dt(slot.get("fcst_at"))
        if fcst_at is None:
            continue
        distance = abs((fcst_at - now_local).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, slot)
    return best[1] if best else None


def _summarize_daypart(label: str, slots: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not slots:
        return None
    temperature_min = _min_float(slot.get("TMP") for slot in slots)
    temperature_max = _max_float(slot.get("TMP") for slot in slots)
    pop_max = _max_int(slot.get("POP") for slot in slots)
    pty_codes = [_to_int(slot.get("PTY")) for slot in slots if _to_int(slot.get("PTY")) is not None]
    sky_codes = [_to_int(slot.get("SKY")) for slot in slots if _to_int(slot.get("SKY")) is not None]
    representative_pty = _dominant_precip_code(pty_codes)
    representative_sky = _dominant_sky_code(sky_codes)
    return {
        "label": label,
        "temperature_min_c": temperature_min,
        "temperature_max_c": temperature_max,
        "precip_probability_max": pop_max,
        "condition_text": _condition_text(
            sky_code=representative_sky,
            pty_code=representative_pty,
        ),
        "precip_type_text": _precip_type_text(representative_pty),
        "sky_text": _sky_text(representative_sky),
    }


def _extract_daily_extreme(slots: list[dict[str, Any]], *, category: str) -> float | None:
    values = [_to_float(slot.get(category)) for slot in slots if _to_float(slot.get(category)) is not None]
    if not values:
        return None
    return float(values[0])


def _combine_kma_datetime(base_date: str, base_time: str, timezone_name: str) -> str | None:
    text_date = str(base_date or "").strip()
    text_time = str(base_time or "").strip()
    if len(text_date) != 8 or len(text_time) != 4:
        return None
    try:
        dt = datetime(
            int(text_date[:4]),
            int(text_date[4:6]),
            int(text_date[6:8]),
            int(text_time[:2]),
            int(text_time[2:4]),
            tzinfo=ZoneInfo(timezone_name),
        )
    except ValueError:
        return None
    return dt.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in {"강수없음", "적설없음"}:
        return 0.0
    if text.endswith("mm 미만"):
        return 0.0
    if text.endswith("cm 미만"):
        return 0.0
    text = text.replace("mm", "").replace("cm", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(round(number))


def _to_precip_mm(value: Any) -> float | None:
    return _to_float(value)


def _precip_amount_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if text == "강수없음":
        return "강수 없음"
    return text


def _precip_type_text(value: Any) -> str:
    code = _to_int(value)
    mapping = {
        0: "강수 없음",
        1: "비",
        2: "비/눈",
        3: "눈",
        4: "소나기",
        5: "빗방울",
        6: "빗방울/눈날림",
        7: "눈날림",
    }
    return mapping.get(code, "강수 없음" if code in (None, 0) else f"강수({code})")


def _sky_text(value: Any) -> str:
    code = _to_int(value)
    mapping = {
        1: "맑음",
        3: "구름많음",
        4: "흐림",
    }
    return mapping.get(code, "정보 없음")


def _condition_text(*, sky_code: Any, pty_code: Any) -> str:
    precip = _precip_type_text(pty_code)
    if precip != "강수 없음":
        return precip
    return _sky_text(sky_code)


def _dominant_precip_code(values: list[int | None]) -> int | None:
    filtered = [value for value in values if value not in (None, 0)]
    if not filtered:
        return 0
    severity_order = {
        1: 4,
        2: 6,
        3: 5,
        4: 3,
        5: 1,
        6: 2,
        7: 1,
    }
    return max(filtered, key=lambda value: severity_order.get(int(value), 0))


def _dominant_sky_code(values: list[int | None]) -> int | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    counts = Counter(filtered)
    return max(counts.keys(), key=lambda value: (counts[value], int(value)))


def _min_float(values: Any) -> float | None:
    cleaned = [value for value in (_to_float(item) for item in values) if value is not None]
    if not cleaned:
        return None
    return round(min(cleaned), 1)


def _max_float(values: Any) -> float | None:
    cleaned = [value for value in (_to_float(item) for item in values) if value is not None]
    if not cleaned:
        return None
    return round(max(cleaned), 1)


def _max_int(values: Any) -> int | None:
    cleaned = [value for value in (_to_int(item) for item in values) if value is not None]
    if not cleaned:
        return None
    return max(cleaned)
