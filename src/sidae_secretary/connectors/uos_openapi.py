from __future__ import annotations

from datetime import datetime
import re
import string
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from dateutil import parser as dt_parser
import requests

from sidae_secretary.connectors.uos_portal import build_uos_timetable_event


UOS_OPENAPI_TIMETABLE_SOURCE = "uos_openapi"
UOS_OPENAPI_BUILDING_SOURCE = "uos_openapi_buildings"
UOS_OPENAPI_OFFICIAL_TIMETABLE_URL = "https://wise.uos.ac.kr/COM/ApiTimeTable/list.do"
UOS_OPENAPI_OFFICIAL_COURSE_PLAN_URL = "https://wise.uos.ac.kr/COM/ApiCoursePlan/list.do"
UOS_OPENAPI_OFFICIAL_BUILDING_URL = "https://wise.uos.ac.kr/COM/ApiBldg/list.do"
_OFFICIAL_METADATA_ALIASES = {
    "official_building_no": (
        "building_no",
        "building_code",
        "building",
        "buildingNo",
        "buildingCode",
        "buildingNumber",
    ),
    "official_building_name": (
        "building_name",
        "building_nm",
        "buildingName",
        "buildingNm",
    ),
    "official_room": (
        "room",
        "classroom",
        "classroom_code",
        "classroom_name",
        "room_no",
        "roomNo",
        "classroomCode",
        "classroomName",
        "lecture_room",
        "lectureRoom",
    ),
    "official_course_code": (
        "course_code",
        "courseCode",
        "subject_code",
        "subjectCode",
        "subject_no",
        "subjectNo",
        "SUBJECT_NO",
        "lecture_code",
        "lectureCode",
        "course_number",
        "courseNumber",
    ),
    "official_subject_no": (
        "subject_no",
        "subjectNo",
        "SUBJECT_NO",
    ),
    "official_syllabus_url": (
        "syllabus_url",
        "syllabusUrl",
        "syllabus_link",
        "syllabusLink",
        "syllabus_href",
        "syllabusHref",
        "lecture_plan_url",
        "lecturePlanUrl",
        "plan_url",
        "course_plan_url",
    ),
    "official_syllabus_id": (
        "syllabus_id",
        "syllabusId",
        "lecture_plan_id",
        "lecturePlanId",
        "plan_id",
        "planId",
    ),
    "official_course_section": (
        "section",
        "class_section",
        "classSection",
        "class_no",
        "classNo",
        "dvcl_no",
        "dvclNo",
        "DVCL_NO",
        "division",
    ),
    "official_dvcl_no": (
        "dvcl_no",
        "dvclNo",
        "DVCL_NO",
    ),
}
_DAY_ALIASES = {
    "1": "MO",
    "2": "TU",
    "3": "WE",
    "4": "TH",
    "5": "FR",
    "6": "SA",
    "7": "SU",
    "mo": "MO",
    "mon": "MO",
    "monday": "MO",
    "tu": "TU",
    "tue": "TU",
    "tues": "TU",
    "tuesday": "TU",
    "we": "WE",
    "wed": "WE",
    "wednesday": "WE",
    "th": "TH",
    "thu": "TH",
    "thur": "TH",
    "thurday": "TH",
    "thursday": "TH",
    "fr": "FR",
    "fri": "FR",
    "friday": "FR",
    "sa": "SA",
    "sat": "SA",
    "saturday": "SA",
    "su": "SU",
    "sun": "SU",
    "sunday": "SU",
    "월": "MO",
    "월요일": "MO",
    "화": "TU",
    "화요일": "TU",
    "수": "WE",
    "수요일": "WE",
    "목": "TH",
    "목요일": "TH",
    "금": "FR",
    "금요일": "FR",
    "토": "SA",
    "토요일": "SA",
    "일": "SU",
    "일요일": "SU",
}
_OFFICIAL_CLASS_SEGMENT_RE = re.compile(
    r"(?P<day>[월화수목금토일])\[(?P<periods>[0-9,\s]+)\]\s*/\s*(?P<location>.*?)(?=(?:\s*[월화수목금토일]\[)|$)"
)


class UOSOpenAPITimetableError(RuntimeError):
    pass


class UOSOpenAPITimetableUnsupported(UOSOpenAPITimetableError):
    pass


class UOSOpenAPITimetableMalformedPayload(UOSOpenAPITimetableError):
    pass


class UOSOpenAPIBuildingCatalogError(RuntimeError):
    pass


class UOSOpenAPIBuildingCatalogMalformedPayload(UOSOpenAPIBuildingCatalogError):
    pass


def uos_openapi_timetable_configured(
    api_url: str | None,
    api_key: str | None = None,
) -> bool:
    return bool(str(api_url or "").strip()) or bool(str(api_key or "").strip())


def uos_openapi_uses_official_catalog_mode(api_url: str | None) -> bool:
    template = str(api_url or "").strip()
    if not template:
        return True
    if "{" in template or "}" in template:
        return False
    parsed = urlparse(template)
    host = str(parsed.netloc or "").strip().lower()
    path = str(parsed.path or "").strip().rstrip("/")
    if not host or not path:
        return False
    return host == "wise.uos.ac.kr" and path == "/COM/ApiTimeTable/list.do"


def resolve_uos_openapi_year_term(
    *,
    academic_year: int | str | None,
    term: int | str | None,
    timezone_name: str,
    current_dt: datetime | None = None,
) -> tuple[int, int]:
    now_local = current_dt or datetime.now(ZoneInfo(timezone_name))
    resolved_year = _int_value(academic_year) or now_local.year
    resolved_term = _normalize_term_code(term)
    if resolved_term is None:
        resolved_term = 20 if now_local.month >= 8 else 10
    return resolved_year, resolved_term


def build_uos_openapi_course_plan_url(
    *,
    api_key: str,
    academic_year: int,
    term_code: int,
    subject_no: str,
    dvcl_no: str,
    base_url: str | None = None,
) -> str:
    return _with_query_params(
        base_url or UOS_OPENAPI_OFFICIAL_COURSE_PLAN_URL,
        {
            "apiKey": str(api_key or "").strip(),
            "year": str(academic_year),
            "term": str(term_code),
            "subjectNo": str(subject_no or "").strip(),
            "dvclNo": str(dvcl_no or "").strip(),
        },
    )


def fetch_uos_openapi_building_catalog(
    *,
    api_key: str | None,
    api_url: str | None = None,
    timeout_sec: int = 15,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    api_key_text = str(api_key or "").strip()
    if not api_key_text:
        raise UOSOpenAPIBuildingCatalogError("UOS official building API key is not configured")
    request_url = _with_query_params(
        str(api_url or "").strip() or UOS_OPENAPI_OFFICIAL_BUILDING_URL,
        {"apiKey": api_key_text},
    )
    headers = {"Accept": "application/json"}
    http = session or requests
    response = http.get(request_url, headers=headers, timeout=max(int(timeout_sec), 1))
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - exercised via malformed payload path
        raise UOSOpenAPIBuildingCatalogMalformedPayload(
            "UOS official building API returned non-JSON data"
        ) from exc
    return normalize_uos_openapi_building_catalog_payload(payload, source_url=request_url)


def fetch_uos_openapi_timetable(
    *,
    api_url: str | None,
    api_key: str | None = None,
    academic_year: int | str | None = None,
    term: int | str | None = None,
    timezone_name: str,
    timeout_sec: int = 15,
    target: dict[str, Any] | None = None,
    session: requests.Session | None = None,
    current_dt: datetime | None = None,
) -> dict[str, Any]:
    request_url: str
    resolved_year: int | None = None
    resolved_term: int | None = None
    if uos_openapi_uses_official_catalog_mode(api_url):
        api_key_text = str(api_key or "").strip()
        if not api_key_text:
            raise UOSOpenAPITimetableUnsupported("UOS official timetable API key is not configured")
        resolved_year, resolved_term = resolve_uos_openapi_year_term(
            academic_year=academic_year,
            term=term,
            timezone_name=timezone_name,
            current_dt=current_dt,
        )
        request_url = _with_query_params(
            str(api_url or "").strip() or UOS_OPENAPI_OFFICIAL_TIMETABLE_URL,
            {
                "apiKey": api_key_text,
                "year": str(resolved_year),
                "term": str(resolved_term),
            },
        )
    else:
        request_url = _resolve_request_url(api_url=api_url, target=target, timezone_name=timezone_name)
        if str(api_key or "").strip():
            request_url = _with_query_params(
                request_url,
                {"apiKey": str(api_key or "").strip()},
            )
    headers = {"Accept": "application/json"}
    http = session or requests
    response = http.get(request_url, headers=headers, timeout=max(int(timeout_sec), 1))
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - exercised via malformed payload path
        raise UOSOpenAPITimetableMalformedPayload("UOS official timetable API returned non-JSON data") from exc
    return normalize_uos_openapi_timetable_payload(
        payload,
        timezone_name=timezone_name,
        source_url=request_url,
        requested_year=resolved_year,
        requested_term=resolved_term,
        api_key=api_key,
        course_plan_url=UOS_OPENAPI_OFFICIAL_COURSE_PLAN_URL,
        current_dt=current_dt,
    )


def normalize_uos_openapi_building_catalog_payload(
    payload: Any,
    *,
    source_url: str | None = None,
) -> dict[str, Any]:
    rows = _extract_building_catalog_rows(payload)
    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise UOSOpenAPIBuildingCatalogMalformedPayload(
                f"UOS official building API row #{index + 1} is not an object"
            )
        building_code = _text_value(
            row.get("building"),
            row.get("BUILDING"),
            row.get("building_code"),
            row.get("buildingCode"),
        )
        building_name = _text_value(
            row.get("building_nm"),
            row.get("BUILDING_NM"),
            row.get("building_name"),
            row.get("buildingName"),
        )
        room_code = _text_value(
            row.get("room_cd"),
            row.get("ROOM_CD"),
            row.get("room_code"),
            row.get("roomCode"),
        )
        room_name = _text_value(
            row.get("room_nm"),
            row.get("ROOM_NM"),
            row.get("room_name"),
            row.get("roomName"),
        )
        space_name = _text_value(
            row.get("spce_nm"),
            row.get("SPCE_NM"),
            row.get("space_name"),
            row.get("spaceName"),
        )
        if not building_code and not building_name:
            continue
        items.append(
            {
                "building_code": building_code,
                "building_name": building_name,
                "room_code": room_code,
                "room_name": room_name,
                "space_name": space_name,
                "raw": dict(row),
            }
        )
    return {
        "ok": True,
        "items": items,
        "payload_source": UOS_OPENAPI_BUILDING_SOURCE,
        "source_url": str(source_url or "").strip() or None,
    }


def normalize_uos_openapi_timetable_payload(
    payload: Any,
    *,
    timezone_name: str,
    source_url: str | None = None,
    requested_year: int | None = None,
    requested_term: int | None = None,
    api_key: str | None = None,
    course_plan_url: str | None = None,
    current_dt: datetime | None = None,
) -> dict[str, Any]:
    anchor_now = current_dt or datetime.now(ZoneInfo(timezone_name))
    root = payload
    official_catalog_mode = False
    if isinstance(root, list):
        events_payload = root
        metadata_root: dict[str, Any] = {}
    elif isinstance(root, dict):
        unsupported_reason = _unsupported_reason(root)
        if unsupported_reason:
            raise UOSOpenAPITimetableUnsupported(unsupported_reason)
        if isinstance(root.get("INFO"), list):
            metadata_root = {}
            events_payload = root.get("INFO")
            official_catalog_mode = True
        else:
            metadata_root, events_payload = _extract_event_container(root)
    else:
        raise UOSOpenAPITimetableMalformedPayload("UOS official timetable API returned an unexpected payload type")
    if not isinstance(events_payload, list):
        raise UOSOpenAPITimetableMalformedPayload("UOS official timetable API payload did not contain an events list")

    default_year = _int_value(
        metadata_root.get("academic_year")
        or metadata_root.get("year")
        or metadata_root.get("current_year")
    ) or _int_value(requested_year)
    default_semester = _semester_value(
        metadata_root.get("semester")
        or metadata_root.get("term")
        or metadata_root.get("current_semester")
        or requested_term
    )
    title = str(metadata_root.get("title") or "학생별강의시간표").strip() or "학생별강의시간표"

    events: list[dict[str, Any]] = []
    for index, item in enumerate(events_payload):
        if official_catalog_mode:
            events.extend(
                _normalize_official_catalog_row(
                    item,
                    timezone_name=timezone_name,
                    current_dt=anchor_now,
                    default_year=default_year,
                    default_semester=default_semester,
                    requested_term=requested_term,
                    api_key=api_key,
                    course_plan_url=course_plan_url,
                    index=index,
                )
            )
            continue
        events.append(
            _normalize_openapi_event(
                item,
                timezone_name=timezone_name,
                current_dt=anchor_now,
                default_year=default_year,
                default_semester=default_semester,
                requested_term=requested_term,
                api_key=api_key,
                course_plan_url=course_plan_url,
                index=index,
            )
        )
    return {
        "ok": True,
        "events": events,
        "title": title,
        "table_count": max(_int_value(metadata_root.get("table_count")) or 0, 1 if events else 0),
        "auth_required": False,
        "source_url": str(source_url or "").strip() or None,
        "payload_source": UOS_OPENAPI_TIMETABLE_SOURCE,
    }


def _normalize_openapi_event(
    item: Any,
    *,
    timezone_name: str,
    current_dt: datetime,
    default_year: int | None,
    default_semester: int | None,
    requested_term: int | None,
    api_key: str | None,
    course_plan_url: str | None,
    index: int,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise UOSOpenAPITimetableMalformedPayload(
            f"UOS official timetable API event #{index + 1} is not an object"
        )
    title = _text_value(
        item.get("title"),
        item.get("course_name"),
        item.get("courseTitle"),
        item.get("subject_name"),
        item.get("subject_nm"),
        item.get("SUBJECT_NM"),
        item.get("subject"),
        item.get("lecture_name"),
        item.get("name"),
    )
    if not title:
        raise UOSOpenAPITimetableMalformedPayload(
            f"UOS official timetable API event #{index + 1} is missing a title"
        )
    location = _text_value(
        item.get("location"),
        item.get("room"),
        item.get("classroom"),
        item.get("place"),
    )
    instructor = _text_value(
        item.get("instructor"),
        item.get("professor"),
        item.get("teacher_name"),
        item.get("teacher"),
        item.get("prof_kor_nm"),
        item.get("PROF_KOR_NM"),
    )
    academic_year = _int_value(item.get("academic_year") or item.get("year") or item.get("YEAR")) or default_year
    term_code = _normalize_term_code(item.get("term") or item.get("TERM") or requested_term)
    semester = _semester_value(item.get("semester") or item.get("term") or item.get("TERM")) or default_semester
    raw_metadata = dict(item.get("metadata")) if isinstance(item.get("metadata"), dict) else {}
    official_metadata = _extract_official_metadata(
        item,
        title=title,
        api_key=api_key,
        academic_year=academic_year,
        term_code=term_code,
        course_plan_url=course_plan_url,
    )

    start_at_raw = _text_value(item.get("start_at"), item.get("start"), item.get("startAt"))
    end_at_raw = _text_value(item.get("end_at"), item.get("end"), item.get("endAt"))
    if start_at_raw and end_at_raw:
        start_at = _parse_datetime(start_at_raw, timezone_name=timezone_name)
        end_at = _parse_datetime(end_at_raw, timezone_name=timezone_name)
        weekday_code = _weekday_code(
            item.get("weekday_code"),
            item.get("weekday"),
            item.get("day"),
            item.get("dow"),
        ) or ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[start_at.weekday()]
        metadata = {**raw_metadata, **official_metadata}
        metadata["academic_year"] = academic_year
        metadata["semester"] = semester
        if term_code is not None:
            metadata["official_term_code"] = term_code
        metadata["weekday_code"] = weekday_code
        if instructor and not str(metadata.get("instructor") or "").strip():
            metadata["instructor"] = instructor
        if str(item.get("source_row") or "").strip():
            metadata["source_row"] = str(item.get("source_row") or "").strip()
        return {
            "external_id": str(item.get("external_id") or "").strip()
            or build_uos_timetable_event(
                weekday_code=weekday_code,
                start_hm=start_at.strftime("%H:%M"),
                end_hm=end_at.strftime("%H:%M"),
                title=title,
                location=location,
                timezone_name=timezone_name,
                current_dt=current_dt,
                academic_year=academic_year,
                semester=semester,
                metadata=metadata,
            )["external_id"],
            "source": "portal",
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "title": title,
            "location": location,
            "rrule": str(item.get("rrule") or "").strip() or f"FREQ=WEEKLY;BYDAY={weekday_code}",
            "metadata": metadata,
        }

    weekday_code = _weekday_code(
        item.get("weekday_code"),
        item.get("weekday"),
        item.get("day"),
        item.get("dow"),
    )
    start_hm = _normalize_hm(
        _text_value(
            item.get("start_hm"),
            item.get("start_time"),
            item.get("startTime"),
            item.get("begin_time"),
        )
    )
    end_hm = _normalize_hm(
        _text_value(
            item.get("end_hm"),
            item.get("end_time"),
            item.get("endTime"),
            item.get("finish_time"),
        )
    )
    if not weekday_code or not start_hm or not end_hm:
        raise UOSOpenAPITimetableMalformedPayload(
            f"UOS official timetable API event #{index + 1} is missing timetable fields"
        )
    metadata = {**raw_metadata, **official_metadata}
    metadata["academic_year"] = academic_year
    metadata["semester"] = semester
    if term_code is not None:
        metadata["official_term_code"] = term_code
    if str(item.get("source_row") or "").strip():
        metadata["source_row"] = str(item.get("source_row") or "").strip()
    return build_uos_timetable_event(
        weekday_code=weekday_code,
        start_hm=start_hm,
        end_hm=end_hm,
        title=title,
        location=location,
        timezone_name=timezone_name,
        current_dt=current_dt,
        academic_year=academic_year,
        semester=semester,
        instructor=instructor,
        metadata=metadata,
    )


def _normalize_official_catalog_row(
    item: Any,
    *,
    timezone_name: str,
    current_dt: datetime,
    default_year: int | None,
    default_semester: int | None,
    requested_term: int | None,
    api_key: str | None,
    course_plan_url: str | None,
    index: int,
) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        raise UOSOpenAPITimetableMalformedPayload(
            f"UOS official timetable API row #{index + 1} is not an object"
        )
    title = _text_value(
        item.get("SUBJECT_NM"),
        item.get("subject_nm"),
        item.get("subject_name"),
        item.get("subject"),
        item.get("course_name"),
        item.get("title"),
    )
    if not title:
        return []
    class_nm = _text_value(item.get("CLASS_NM"), item.get("class_nm"))
    if not class_nm:
        return []
    instructor = _text_value(item.get("PROF_KOR_NM"), item.get("prof_kor_nm"), item.get("instructor"))
    academic_year = _int_value(item.get("YEAR") or item.get("year")) or default_year
    term_code = _normalize_term_code(item.get("TERM") or item.get("term") or requested_term)
    semester = _semester_value(item.get("TERM") or item.get("term")) or default_semester
    raw_metadata = dict(item.get("metadata")) if isinstance(item.get("metadata"), dict) else {}
    official_metadata = _extract_official_metadata(
        item,
        title=title,
        api_key=api_key,
        academic_year=academic_year,
        term_code=term_code,
        course_plan_url=course_plan_url,
    )
    output: list[dict[str, Any]] = []
    for meeting_index, meeting in enumerate(_parse_official_class_nm(class_nm), start=1):
        metadata = {
            **raw_metadata,
            **official_metadata,
            "academic_year": academic_year,
            "semester": semester,
            "official_term_code": term_code,
            "weekday_code": meeting["weekday_code"],
            "class_nm": class_nm,
            "catalog_source_row": index + 1,
            "catalog_meeting_index": meeting_index,
        }
        if instructor and not str(metadata.get("instructor") or "").strip():
            metadata["instructor"] = instructor
        output.append(
            build_uos_timetable_event(
                weekday_code=meeting["weekday_code"],
                start_hm=meeting["start_hm"],
                end_hm=meeting["end_hm"],
                title=title,
                location=meeting["location"],
                timezone_name=timezone_name,
                current_dt=current_dt,
                academic_year=academic_year,
                semester=semester,
                instructor=instructor,
                metadata=metadata,
            )
        )
    return output


def _resolve_request_url(
    *,
    api_url: str | None,
    target: dict[str, Any] | None,
    timezone_name: str,
) -> str:
    template = str(api_url or "").strip()
    if not template:
        raise UOSOpenAPITimetableUnsupported("UOS official timetable API URL is not configured")
    context = _request_context(target=target, timezone_name=timezone_name)
    formatter = string.Formatter()
    required_fields = {
        field_name
        for _, field_name, _, _ in formatter.parse(template)
        if field_name
    }
    missing = sorted(field for field in required_fields if field not in context)
    if missing:
        raise UOSOpenAPITimetableUnsupported(
            f"UOS official timetable API request is unsupported for this target; missing {', '.join(missing)}"
        )
    return template.format_map(context)


def _extract_official_metadata(
    item: dict[str, Any],
    *,
    title: str,
    api_key: str | None = None,
    academic_year: int | None = None,
    term_code: int | None = None,
    course_plan_url: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for target_key, source_keys in _OFFICIAL_METADATA_ALIASES.items():
        value = _text_value(*(item.get(source_key) for source_key in source_keys))
        if value:
            metadata[target_key] = value
    metadata.setdefault("official_course_name", str(title or "").strip())
    subject_no = str(metadata.get("official_subject_no") or "").strip()
    dvcl_no = str(metadata.get("official_dvcl_no") or "").strip()
    if subject_no and not str(metadata.get("official_course_code") or "").strip():
        metadata["official_course_code"] = subject_no
    if dvcl_no and not str(metadata.get("official_course_section") or "").strip():
        metadata["official_course_section"] = dvcl_no
    if (
        subject_no
        and dvcl_no
        and str(api_key or "").strip()
        and academic_year is not None
        and term_code is not None
        and not str(metadata.get("official_syllabus_url") or "").strip()
    ):
        metadata["official_syllabus_url"] = build_uos_openapi_course_plan_url(
            api_key=str(api_key or "").strip(),
            academic_year=academic_year,
            term_code=term_code,
            subject_no=subject_no,
            dvcl_no=dvcl_no,
            base_url=course_plan_url,
        )
    if (
        subject_no
        and dvcl_no
        and academic_year is not None
        and term_code is not None
        and not str(metadata.get("official_syllabus_id") or "").strip()
    ):
        metadata["official_syllabus_id"] = f"{academic_year}:{term_code}:{subject_no}:{dvcl_no}"
    return metadata


def _request_context(
    *,
    target: dict[str, Any] | None,
    timezone_name: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    payload = target if isinstance(target, dict) else {}
    for key, value in payload.items():
        _store_scalar(context, str(key), value)
    session_metadata = payload.get("session_metadata")
    if isinstance(session_metadata, dict):
        for key, value in session_metadata.items():
            if key == "browser_result" and isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    _store_scalar(context, f"browser_{nested_key}", nested_value)
                continue
            _store_scalar(context, str(key), value)
    now_local = datetime.now(ZoneInfo(timezone_name))
    context.setdefault("timezone", timezone_name)
    context.setdefault("today", now_local.date().isoformat())
    context.setdefault("current_year", now_local.year)
    context.setdefault("current_month", now_local.month)
    context.setdefault("current_day", now_local.day)
    return context


def _store_scalar(target: dict[str, Any], key: str, value: Any) -> None:
    if not key:
        return
    if isinstance(value, bool):
        target[key] = value
        return
    if isinstance(value, (int, float)):
        target[key] = value
        return
    if value is None:
        return
    text = str(value).strip()
    if text:
        target[key] = text


def _extract_event_container(payload: dict[str, Any]) -> tuple[dict[str, Any], list[Any] | None]:
    if isinstance(payload.get("events"), list):
        return payload, payload.get("events")
    if isinstance(payload.get("timetable"), list):
        return payload, payload.get("timetable")
    for container_key in ("data", "result", "payload", "response"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            unsupported_reason = _unsupported_reason(nested)
            if unsupported_reason:
                raise UOSOpenAPITimetableUnsupported(unsupported_reason)
            if isinstance(nested.get("events"), list):
                return {**payload, **nested}, nested.get("events")
            if isinstance(nested.get("timetable"), list):
                return {**payload, **nested}, nested.get("timetable")
        if isinstance(nested, list):
            return payload, nested
    return payload, None


def _extract_building_catalog_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise UOSOpenAPIBuildingCatalogMalformedPayload(
            "UOS official building API returned an unexpected payload type"
        )
    if isinstance(payload.get("INFO"), list):
        return payload.get("INFO") or []
    if isinstance(payload.get("items"), list):
        return payload.get("items") or []
    for container_key in ("data", "result", "payload", "response"):
        nested = payload.get(container_key)
        if isinstance(nested, list):
            return nested
        if not isinstance(nested, dict):
            continue
        if isinstance(nested.get("INFO"), list):
            return nested.get("INFO") or []
        if isinstance(nested.get("items"), list):
            return nested.get("items") or []
    raise UOSOpenAPIBuildingCatalogMalformedPayload(
        "UOS official building API payload did not contain a rows list"
    )


def _unsupported_reason(payload: dict[str, Any]) -> str | None:
    if payload.get("supported") is False or payload.get("unsupported") is True:
        return _text_value(
            payload.get("reason"),
            payload.get("message"),
            payload.get("error"),
        ) or "UOS official timetable API does not support this request"
    status = str(payload.get("status") or "").strip().lower()
    if status in {"unsupported", "not_supported", "not-supported"}:
        return _text_value(
            payload.get("reason"),
            payload.get("message"),
            payload.get("error"),
        ) or "UOS official timetable API does not support this request"
    return None


def _parse_datetime(value: str, *, timezone_name: str) -> datetime:
    try:
        parsed = dt_parser.isoparse(value)
    except Exception as exc:
        raise UOSOpenAPITimetableMalformedPayload(
            f"invalid timetable datetime: {value}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def _weekday_code(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if text in _DAY_ALIASES:
            return _DAY_ALIASES[text]
        upper = text.upper()
        if upper in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}:
            return upper
    return None


def _normalize_hm(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_official_class_nm(value: str) -> list[dict[str, str | None]]:
    output: list[dict[str, str | None]] = []
    text = str(value or "").strip()
    if not text:
        return output
    for match in _OFFICIAL_CLASS_SEGMENT_RE.finditer(text):
        weekday_code = _weekday_code(match.group("day"))
        period_numbers = [int(part) for part in re.findall(r"\d+", str(match.group("periods") or ""))]
        if not weekday_code or not period_numbers:
            continue
        start_hm, end_hm = _period_range_to_hm(period_numbers)
        if not start_hm or not end_hm:
            continue
        output.append(
            {
                "weekday_code": weekday_code,
                "start_hm": start_hm,
                "end_hm": end_hm,
                "location": str(match.group("location") or "").strip() or None,
            }
        )
    return output


def _period_range_to_hm(period_numbers: list[int]) -> tuple[str | None, str | None]:
    cleaned = sorted({period for period in period_numbers if period > 0})
    if not cleaned:
        return None, None
    start_hm = _period_to_hm(cleaned[0], end=False)
    end_hm = _period_to_hm(cleaned[-1], end=True)
    return start_hm, end_hm


def _period_to_hm(period: int, *, end: bool) -> str | None:
    if period <= 0:
        return None
    offset_minutes = (period - 1) * 60 + (50 if end else 0)
    hour = 9 + (offset_minutes // 60)
    minute = offset_minutes % 60
    return f"{hour:02d}:{minute:02d}"


def _normalize_term_code(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"1학기", "1", "10", "spring", "first"}:
        return 10
    if text in {"2학기", "2", "20", "fall", "second"}:
        return 20
    parsed = _int_value(value)
    if parsed == 1:
        return 10
    if parsed == 2:
        return 20
    if parsed in {10, 20}:
        return parsed
    return None


def _semester_value(value: Any) -> int | None:
    code = _normalize_term_code(value)
    if code == 10:
        return 1
    if code == 20:
        return 2
    parsed = _int_value(value)
    if parsed in {1, 2}:
        return parsed
    return None


def _with_query_params(url: str, params: dict[str, Any]) -> str:
    parsed = urlparse(str(url or "").strip() or UOS_OPENAPI_OFFICIAL_TIMETABLE_URL)
    current = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        text = str(value or "").strip()
        if text:
            current[str(key)] = text
    return urlunparse(parsed._replace(query=urlencode(current)))


def _text_value(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _int_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
