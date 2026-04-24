from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import csv
import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dateutil import parser as dt_parser
from icalendar import Calendar
import requests


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "event"


def _coerce_datetime(value: Any, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        raise ValueError(f"unsupported date value: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


@dataclass
class PortalEvent:
    external_id: str
    start_at: str
    end_at: str
    title: str
    location: str | None
    rrule: str | None
    metadata: dict[str, Any]


@dataclass
class PortalNotice:
    seq: str
    title: str
    department: str | None
    posted_on: str | None
    list_id: str
    menuid: str
    sort: str | None = None
    source_url: str | None = None
    article_url: str | None = None


@dataclass
class PortalNoticeFetchMetadata:
    list_id: str
    menuid: str
    requested_limit: int
    requested_at: str
    source_url: str
    resolved_url: str
    fetched_at: str | None = None
    http_status: int | None = None
    page_title: str | None = None
    parser: str = "uos_notice_regex_v2"
    parsed_count: int = 0
    empty_detected: bool = False


@dataclass
class PortalNoticeFetchResult:
    notices: list[PortalNotice]
    metadata: PortalNoticeFetchMetadata


class PortalNoticeFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        metadata: PortalNoticeFetchMetadata | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if isinstance(metadata, PortalNoticeFetchMetadata):
            self.metadata = asdict(metadata)
        elif isinstance(metadata, dict):
            self.metadata = dict(metadata)
        else:
            self.metadata = {}


UOS_NOTICE_LIST_URL = "https://www.uos.ac.kr/korNotice/list.do"
UOS_NOTICE_VIEW_URL = "https://www.uos.ac.kr/korNotice/view.do"
UOS_NOTICE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.uos.ac.kr/",
}
UOS_NOTICE_ITEM_RE = re.compile(
    r"<div class=\"ti\">\s*"
    r"<a href=\"javascript:fnView\('(?P<sort>\d+)',\s*'(?P<seq>\d+)'\);\">\s*(?P<title>.*?)</a>\s*"
    r"</div>\s*"
    r"<div class=\"da\">\s*"
    r"<span>(?P<department>.*?)</span>\s*"
    r"<span>\s*(?P<posted_on>\d{4}-\d{2}-\d{2})\s*</span>",
    re.DOTALL,
)
UOS_NOTICE_EMPTY_RE = re.compile(r"게시글이\s*없습니다|등록된\s*게시물(?:이)?\s*없습니다")
UOS_NOTICE_TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)


def _rrule_to_string(rrule_value: Any) -> str | None:
    if rrule_value is None:
        return None
    try:
        return rrule_value.to_ical().decode("utf-8")
    except Exception:
        return str(rrule_value)


def _portal_external_id(title: str, start_at: datetime) -> str:
    slug = _slugify(title)
    return f"portal:{slug}:{start_at.date().isoformat()}"


def _clean_portal_notice_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _uos_notice_params(list_id: str, menuid: str) -> dict[str, str]:
    return {
        "identified": "anonymous",
        "list_id": str(list_id).strip(),
        "menuid": str(menuid).strip(),
        "pageIndex": "1",
        "searchCnd": "1",
        "searchWrd": "",
        "viewAuth": "Y",
        "writeAuth": "N",
    }


def _uos_notice_list_url(params: dict[str, str]) -> str:
    return f"{UOS_NOTICE_LIST_URL}?{urlencode(params)}"


def _uos_notice_view_url(*, seq: str, sort: str | None, params: dict[str, str]) -> str:
    query = {
        **params,
        "seq": str(seq).strip(),
    }
    if str(sort or "").strip():
        query["sort"] = str(sort).strip()
    return f"{UOS_NOTICE_VIEW_URL}?{urlencode(query)}"


def _uos_notice_page_title(text: str) -> str | None:
    match = UOS_NOTICE_TITLE_RE.search(str(text or ""))
    if not match:
        return None
    return _clean_portal_notice_text(match.group("title")) or None


def _uos_notice_page_is_empty(text: str) -> bool:
    return bool(UOS_NOTICE_EMPTY_RE.search(_clean_portal_notice_text(text)))


def parse_ics_text(ics_text: str, timezone_name: str) -> list[PortalEvent]:
    calendar = Calendar.from_ical(ics_text)
    events: dict[str, PortalEvent] = {}
    for component in calendar.walk("VEVENT"):
        summary = str(component.get("SUMMARY", "")).strip() or "Academic Calendar"
        location_raw = component.get("LOCATION")
        location = str(location_raw).strip() if location_raw else None

        dtstart_prop = component.get("DTSTART")
        if not dtstart_prop:
            continue
        start_dt = _coerce_datetime(dtstart_prop.dt, timezone_name)

        dtend_prop = component.get("DTEND")
        if dtend_prop:
            end_dt = _coerce_datetime(dtend_prop.dt, timezone_name)
        elif isinstance(dtstart_prop.dt, date) and not isinstance(
            dtstart_prop.dt, datetime
        ):
            end_dt = start_dt + timedelta(days=1)
        else:
            end_dt = start_dt + timedelta(hours=1)

        external_id = _portal_external_id(summary, start_dt)
        events[external_id] = PortalEvent(
            external_id=external_id,
            start_at=start_dt.isoformat(),
            end_at=end_dt.isoformat(),
            title=summary,
            location=location,
            rrule=_rrule_to_string(component.get("RRULE")),
            metadata={"uid": str(component.get("UID", "")).strip() or None},
        )
    return list(events.values())


def fetch_uos_notice_feed(
    *,
    list_id: str,
    menuid: str,
    limit: int = 10,
    timeout_sec: int = 30,
) -> PortalNoticeFetchResult:
    params = _uos_notice_params(str(list_id).strip(), str(menuid).strip())
    metadata = PortalNoticeFetchMetadata(
        list_id=params["list_id"],
        menuid=params["menuid"],
        requested_limit=max(int(limit), 1),
        requested_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        source_url=_uos_notice_list_url(params),
        resolved_url=_uos_notice_list_url(params),
    )
    try:
        response = requests.get(
            UOS_NOTICE_LIST_URL,
            params=params,
            headers=UOS_NOTICE_HEADERS,
            timeout=timeout_sec,
        )
        metadata.resolved_url = str(getattr(response, "url", "") or metadata.source_url).strip() or metadata.source_url
        metadata.http_status = getattr(response, "status_code", None)
        response.raise_for_status()
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            metadata.resolved_url = (
                str(getattr(response, "url", "") or metadata.resolved_url).strip()
                or metadata.source_url
            )
            metadata.http_status = getattr(response, "status_code", None)
        raise PortalNoticeFetchError(
            f"UOS notice request failed: {str(exc).strip() or exc.__class__.__name__}",
            metadata=metadata,
        ) from exc

    notices: list[PortalNotice] = []
    seen_seq: set[str] = set()
    metadata.fetched_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    metadata.page_title = _uos_notice_page_title(response.text)
    for match in UOS_NOTICE_ITEM_RE.finditer(response.text):
        sort = str(match.group("sort") or "").strip() or None
        seq = str(match.group("seq") or "").strip()
        title = _clean_portal_notice_text(match.group("title"))
        if not seq or not title or seq in seen_seq:
            continue
        seen_seq.add(seq)
        notices.append(
            PortalNotice(
                seq=seq,
                title=title,
                department=_clean_portal_notice_text(match.group("department")) or None,
                posted_on=str(match.group("posted_on") or "").strip() or None,
                list_id=str(list_id).strip(),
                menuid=str(menuid).strip(),
                sort=sort,
                source_url=metadata.resolved_url,
                article_url=_uos_notice_view_url(seq=seq, sort=sort, params=params),
            )
        )
        if len(notices) >= max(int(limit), 1):
            break
    metadata.parsed_count = len(notices)
    metadata.empty_detected = len(notices) == 0 and _uos_notice_page_is_empty(response.text)
    if not notices and not metadata.empty_detected:
        raise PortalNoticeFetchError(
            "UOS notice page returned no parsable notice items",
            metadata=metadata,
        )
    return PortalNoticeFetchResult(notices=notices, metadata=metadata)


def fetch_uos_notice_titles(
    *,
    list_id: str,
    menuid: str,
    limit: int = 10,
    timeout_sec: int = 30,
) -> list[PortalNotice]:
    return fetch_uos_notice_feed(
        list_id=list_id,
        menuid=menuid,
        limit=limit,
        timeout_sec=timeout_sec,
    ).notices


def parse_ics_file(path: Path, timezone_name: str) -> list[PortalEvent]:
    text = path.read_text(encoding="utf-8")
    return parse_ics_text(text, timezone_name=timezone_name)


def parse_ics_url(url: str, timezone_name: str, timeout_sec: int = 30) -> list[PortalEvent]:
    response = requests.get(url, timeout=timeout_sec)
    response.raise_for_status()
    return parse_ics_text(response.text, timezone_name=timezone_name)


def parse_csv_file(path: Path, timezone_name: str) -> list[PortalEvent]:
    tz = ZoneInfo(timezone_name)
    events: dict[str, PortalEvent] = {}
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            raw_date = str(row.get("date") or "").strip()
            title = str(row.get("title") or "").strip()
            if not raw_date or not title:
                continue
            parsed = dt_parser.parse(raw_date)
            start_dt = datetime.combine(parsed.date(), time.min).replace(tzinfo=tz)
            end_dt = start_dt + timedelta(days=1)
            external_id = _portal_external_id(title, start_dt)
            events[external_id] = PortalEvent(
                external_id=external_id,
                start_at=start_dt.isoformat(),
                end_at=end_dt.isoformat(),
                title=title,
                location=None,
                rrule=None,
                metadata={"source": "csv", "raw_date": raw_date},
            )
    return list(events.values())
