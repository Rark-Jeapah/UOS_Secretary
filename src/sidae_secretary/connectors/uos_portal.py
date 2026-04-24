from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha1
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from sidae_secretary.browser_session import ensure_private_directory

logger = logging.getLogger(__name__)

UOS_PORTAL_SCHOOL_SLUG = "uos_portal"
UOS_PORTAL_PROVIDER = "uos_portal"
UOS_PORTAL_LOGIN_URL = "https://portal.uos.ac.kr/p/STUD/"
UOS_WISE_INDEX_URL = "https://wise.uos.ac.kr/index.do"
UOS_WISE_ENTRY_URL = "https://wise.uos.ac.kr/exsignon/sso/sso_index.jsp?RelayState=/index.do"
UOS_PORTAL_LECTURE_SCHEDULE_URL = None
UOS_PORTAL_AUTH_REQUIRED_PATH = "/svc/tk/auth.eps"
UOS_TIMETABLE_TITLE = "학생별강의시간표"
UOS_TIMETABLE_SURFACE_LABELS = (
    UOS_TIMETABLE_TITLE,
    "학생별 강의시간표",
)
UOS_TIMETABLE_NAVIGATION_LABELS = (
    UOS_TIMETABLE_TITLE,
    "강의시간표",
)
UOS_TIMETABLE_MENU_ITEM_IDS: tuple[str, ...] = ()
UOS_TIMETABLE_MENU_PARENT_IDS = (
    "USTD000.040",
)
UOS_TIMETABLE_BLOCKED_LABELS = (
    "전공/교양/통섭 시간표조회",
    "check major/elective/consilience schedule",
)
UOS_PORTAL_USERNAME_SELECTORS = (
    "input[name='userId']",
    "input[id='userId']",
    "input[name='userid']",
    "input[id='userid']",
    "input[name='username']",
    "input[id='username']",
    "input[name='user_id']",
    "input[id='user_id']",
    "input[type='text']",
)
UOS_PORTAL_PASSWORD_SELECTORS = (
    "input[name='password']",
    "input[id='password']",
    "input[name='passwd']",
    "input[id='passwd']",
    "input[name='userPw']",
    "input[id='userPw']",
    "input[type='password']",
)
UOS_PORTAL_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('로그인')",
    "a:has-text('로그인')",
)
UOS_DAY_LABELS = {
    "월": "MO",
    "화": "TU",
    "수": "WE",
    "목": "TH",
    "금": "FR",
    "토": "SA",
    "일": "SU",
}
TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2})\s*(?:-|~|～|–|—)\s*(?P<end>\d{1,2}:\d{2})"
)
PERIOD_LABEL_RE = re.compile(r"^\d+\s*교시$")
LOCATION_RE = re.compile(r"(?P<building>\d+)\s*-\s*(?P<room>[A-Za-z]?\d+(?:[.-]\d+)?)")
YEAR_SEMESTER_RE = re.compile(r"(?P<year>20\d{2})\s*학년도\s*(?P<semester>\d)\s*학기")
PERIOD_NUMBER_RE = re.compile(r"^(?P<period>\d+)\s*교시$")


@dataclass
class UOSTableCell:
    key: str
    text: str
    rowspan: int = 1
    colspan: int = 1


def extract_year_semester(value: str) -> tuple[int | None, int | None]:
    match = YEAR_SEMESTER_RE.search(str(value or ""))
    if not match:
        return None, None
    try:
        return int(match.group("year")), int(match.group("semester"))
    except (TypeError, ValueError):
        return None, None


def build_uos_timetable_external_id(
    *,
    academic_year: int | None,
    semester: int | None,
    weekday_code: str,
    start_hm: str,
    end_hm: str,
    title: str,
    location: str | None,
) -> str:
    external_seed = "|".join(
        [
            str(academic_year or ""),
            str(semester or ""),
            str(weekday_code or "").strip().upper(),
            str(start_hm or "").strip(),
            str(end_hm or "").strip(),
            str(title or "").strip(),
            str(location or "").strip(),
        ]
    )
    return f"portal:uos:timetable:{sha1(external_seed.encode('utf-8')).hexdigest()[:24]}"


def build_uos_timetable_event(
    *,
    weekday_code: str,
    start_hm: str,
    end_hm: str,
    title: str,
    timezone_name: str,
    current_dt: datetime | None = None,
    location: str | None = None,
    academic_year: int | None = None,
    semester: int | None = None,
    instructor: str | None = None,
    metadata: dict[str, Any] | None = None,
    source: str = "portal",
) -> dict[str, Any]:
    normalized_weekday = str(weekday_code or "").strip().upper()
    if normalized_weekday not in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}:
        raise ValueError("weekday_code must be a weekly RRULE code")
    normalized_title = str(title or "").strip()
    if not normalized_title:
        raise ValueError("title is required")
    anchor_now = current_dt or datetime.now(ZoneInfo(timezone_name))
    start_at, end_at = _meeting_datetimes(
        weekday_code=normalized_weekday,
        start_hm=start_hm,
        end_hm=end_hm,
        timezone_name=timezone_name,
        current_dt=anchor_now,
    )
    event_metadata = dict(metadata or {})
    event_metadata["school_slug"] = UOS_PORTAL_SCHOOL_SLUG
    event_metadata["timetable_source"] = UOS_PORTAL_SCHOOL_SLUG
    event_metadata["portal_provider"] = str(
        event_metadata.get("portal_provider") or UOS_PORTAL_PROVIDER
    ).strip() or UOS_PORTAL_PROVIDER
    event_metadata["academic_year"] = academic_year
    event_metadata["semester"] = semester
    event_metadata["weekday_code"] = normalized_weekday
    if instructor and not str(event_metadata.get("instructor") or "").strip():
        event_metadata["instructor"] = instructor
    return {
        "external_id": build_uos_timetable_external_id(
            academic_year=academic_year,
            semester=semester,
            weekday_code=normalized_weekday,
            start_hm=start_at.strftime("%H:%M"),
            end_hm=end_at.strftime("%H:%M"),
            title=normalized_title,
            location=location,
        ),
        "source": str(source or "portal").strip() or "portal",
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "title": normalized_title,
        "location": str(location or "").strip() or None,
        "rrule": f"FREQ=WEEKLY;BYDAY={normalized_weekday}",
        "metadata": event_metadata,
    }


def parse_uos_timetable_tables(
    tables: list[list[list[dict[str, Any]]]],
    *,
    timezone_name: str,
    current_dt: datetime | None = None,
    year: int | None = None,
    semester: int | None = None,
) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen_external_ids: set[str] = set()
    for table in tables:
        for meeting in parse_uos_timetable_table(
            table,
            timezone_name=timezone_name,
            current_dt=current_dt,
            year=year,
            semester=semester,
        ):
            external_id = str(meeting.get("external_id") or "").strip()
            if not external_id or external_id in seen_external_ids:
                continue
            seen_external_ids.add(external_id)
            parsed.append(meeting)
    return parsed


def parse_uos_timetable_table(
    table: list[list[dict[str, Any]]],
    *,
    timezone_name: str,
    current_dt: datetime | None = None,
    year: int | None = None,
    semester: int | None = None,
) -> list[dict[str, Any]]:
    expanded = _expand_table(table)
    if not expanded:
        return []
    day_header_row = _find_day_header_row(expanded)
    if day_header_row is None:
        return []
    day_columns = _day_column_map(expanded[day_header_row])
    if len(day_columns) < 5:
        return []
    time_column = _find_time_column(expanded, day_header_row=day_header_row)
    if time_column is None:
        return []
    row_times = _row_time_ranges(expanded, time_column=time_column, day_header_row=day_header_row)
    if not row_times:
        return []

    anchor_now = current_dt or datetime.now(ZoneInfo(timezone_name))
    extracted_year = year
    extracted_semester = semester
    if extracted_year is None or extracted_semester is None:
        flattened = " ".join(_cell_text(cell) for row in expanded for cell in row if cell is not None)
        year_guess, semester_guess = extract_year_semester(flattened)
        extracted_year = extracted_year if extracted_year is not None else year_guess
        extracted_semester = extracted_semester if extracted_semester is not None else semester_guess

    positions_by_key: dict[str, list[tuple[int, int]]] = {}
    text_by_key: dict[str, str] = {}
    for row_idx, row in enumerate(expanded):
        for col_idx, cell in enumerate(row):
            if cell is None or row_idx <= day_header_row:
                continue
            text = _cell_text(cell)
            if not text:
                continue
            if col_idx not in day_columns:
                continue
            if TIME_RANGE_RE.search(text) or PERIOD_LABEL_RE.match(text):
                continue
            key = str(cell.get("key") or f"{row_idx}:{col_idx}")
            positions_by_key.setdefault(key, []).append((row_idx, col_idx))
            text_by_key[key] = text

    results: list[dict[str, Any]] = []
    for key, positions in positions_by_key.items():
        min_row = min(item[0] for item in positions)
        max_row = max(item[0] for item in positions)
        min_col = min(item[1] for item in positions)
        max_col = max(item[1] for item in positions)
        if min_col != max_col or min_col not in day_columns:
            continue
        start_range = row_times.get(min_row)
        end_range = row_times.get(max_row)
        if not start_range or not end_range:
            continue
        weekday_code = day_columns[min_col]
        text = text_by_key.get(key) or ""
        title, location, instructor, detail_lines = _parse_class_cell_text(text)
        if not title:
            continue
        results.append(
            build_uos_timetable_event(
                weekday_code=weekday_code,
                start_hm=start_range[0],
                end_hm=end_range[1],
                title=title,
                location=location,
                timezone_name=timezone_name,
                current_dt=anchor_now,
                academic_year=extracted_year,
                semester=extracted_semester,
                instructor=instructor,
                metadata={
                    "period_start_row": min_row,
                    "period_end_row": max_row,
                    "cell_text": text,
                    "detail_lines": detail_lines,
                },
            )
        )
    results.sort(key=lambda item: (str(item["start_at"]), str(item["title"]).lower()))
    return results


def fetch_uos_portal_timetable(
    *,
    storage_state: str | dict[str, Any] | None = None,
    profile_dir: Path | None = None,
    current_url: str | None,
    timezone_name: str,
    browser_channel: str = "",
    browser_executable_path: Path | None = None,
    headless: bool = True,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    sync_playwright = _load_playwright_sync_api()
    target_url = str(current_url or "").strip() or UOS_WISE_INDEX_URL
    playwright = sync_playwright().start()
    browser = None
    context = None
    page = None
    network_samples: list[dict[str, Any]] = []
    try:
        launch_kwargs: dict[str, Any] = {"headless": bool(headless)}
        executable = (
            Path(browser_executable_path).expanduser().resolve()
            if browser_executable_path is not None
            else None
        )
        if executable is not None:
            launch_kwargs["executable_path"] = str(executable)
        else:
            channel = str(browser_channel or "").strip()
            if channel:
                launch_kwargs["channel"] = channel
        storage_state_payload: dict[str, Any] | None = None
        if isinstance(storage_state, dict):
            storage_state_payload = storage_state
        elif str(storage_state or "").strip():
            storage_state_payload = _parse_storage_state_payload(str(storage_state))
        if storage_state_payload is None and profile_dir:
            resolved_profile_dir = Path(profile_dir).expanduser().resolve()
            if not resolved_profile_dir.exists():
                raise FileNotFoundError(f"profile dir missing: {resolved_profile_dir}")
            launch_kwargs["user_data_dir"] = str(resolved_profile_dir)
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        else:
            browser = playwright.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {}
            if storage_state_payload is not None:
                context_kwargs["storage_state"] = storage_state_payload
            context = browser.new_context(**context_kwargs)
        context.on("response", lambda response: _record_network_sample(network_samples, response))
        page = context.pages[0] if context.pages else context.new_page()
        return _fetch_uos_portal_timetable_from_page(
            page,
            timezone_name=timezone_name,
            timeout_sec=timeout_sec,
            target_url=target_url,
            navigate_before_fetch=True,
            network_samples=network_samples,
        )
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass


def login_uos_portal_browser_session(
    *,
    username: str,
    password: str,
    profile_dir: Path | None = None,
    prefetch_timetable: bool = False,
    timezone_name: str = "Asia/Seoul",
    browser_channel: str = "",
    browser_executable_path: Path | None = None,
    headless: bool = True,
    timeout_sec: int = 45,
) -> dict[str, Any]:
    sync_playwright = _load_playwright_sync_api()
    user = str(username or "").strip()
    secret = str(password or "")
    if not user:
        raise ValueError("UOS portal username is required")
    if not secret:
        raise ValueError("UOS portal password is required")

    playwright = sync_playwright().start()
    browser = None
    context = None
    page = None
    network_samples: list[dict[str, Any]] = []
    resolved_profile_dir = (
        ensure_private_directory(Path(profile_dir).expanduser().resolve())
        if profile_dir is not None
        else None
    )
    try:
        launch_kwargs: dict[str, Any] = {"headless": bool(headless)}
        executable = (
            Path(browser_executable_path).expanduser().resolve()
            if browser_executable_path is not None
            else None
        )
        if executable is not None:
            launch_kwargs["executable_path"] = str(executable)
        else:
            channel = str(browser_channel or "").strip()
            if channel:
                launch_kwargs["channel"] = channel
        if resolved_profile_dir is not None:
            launch_kwargs["user_data_dir"] = str(resolved_profile_dir)
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        else:
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context()
        context.on("response", lambda response: _record_network_sample(network_samples, response))
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(UOS_PORTAL_LOGIN_URL, wait_until="domcontentloaded", timeout=max(int(timeout_sec), 1) * 1000)
        page.wait_for_timeout(1000)

        username_selector = _fill_first_visible(page, UOS_PORTAL_USERNAME_SELECTORS, user)
        password_selector = _fill_first_visible(page, UOS_PORTAL_PASSWORD_SELECTORS, secret)
        if not username_selector or not password_selector:
            raise RuntimeError("UOS portal login form not found")

        if not _click_first_visible(page, UOS_PORTAL_SUBMIT_SELECTORS):
            try:
                page.locator(password_selector).first.press("Enter")
            except Exception as exc:
                raise RuntimeError("UOS portal login submit failed") from exc

        _settle_after_login(page, timeout_sec=timeout_sec)
        page.goto(UOS_WISE_ENTRY_URL, wait_until="domcontentloaded", timeout=max(int(timeout_sec), 1) * 1000)
        _settle_after_login(page, timeout_sec=timeout_sec)
        page_text = _safe_page_text(page)
        current_url = str(getattr(page, "url", "") or "").strip()
        title = _safe_page_title(page)
        if _looks_like_login_failure(current_url=current_url, title=title, page_text=page_text, page=page):
            raise RuntimeError("UOS portal login failed")
        result = {
            "current_url": current_url,
            "title": title,
        }
        if prefetch_timetable:
            network_samples.clear()
            timetable_fetch = _fetch_uos_portal_timetable_from_page(
                page,
                timezone_name=str(timezone_name or "Asia/Seoul") or "Asia/Seoul",
                timeout_sec=timeout_sec,
                target_url=current_url or UOS_WISE_ENTRY_URL,
                navigate_before_fetch=False,
                network_samples=network_samples,
            )
            result["timetable_fetch"] = timetable_fetch
            fetched_current_url = str(timetable_fetch.get("current_url") or "").strip()
            fetched_title = str(timetable_fetch.get("title") or "").strip()
            if fetched_current_url:
                result["current_url"] = fetched_current_url
            if fetched_title:
                result["title"] = fetched_title
        if resolved_profile_dir is not None:
            try:
                cookies = context.cookies()
            except Exception:
                cookies = []
            result["profile_dir"] = str(resolved_profile_dir)
            result["cookie_count"] = len(list(cookies or []))
            return result
        storage_state = context.storage_state()
        result["storage_state"] = storage_state
        result["cookie_count"] = (
            len(list(storage_state.get("cookies") or []))
            if isinstance(storage_state, dict)
            else 0
        )
        return result
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass


def _parse_storage_state_payload(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "").strip())
    except Exception as exc:
        raise RuntimeError("invalid UOS portal session state") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid UOS portal session state")
    return parsed


def _record_network_sample(network_samples: list[dict[str, Any]], response: Any) -> None:
    if len(network_samples) >= 50:
        return
    try:
        resource_type = str(response.request.resource_type or "").strip().lower()
    except Exception:
        resource_type = ""
    if resource_type not in {"xhr", "fetch"}:
        return
    try:
        url = str(response.url or "").strip()
    except Exception:
        url = ""
    if not url:
        return
    network_samples.append(
        {
            "url": _sanitize_url_without_query(url),
            "status": int(getattr(response, "status", 0) or 0),
            "resource_type": resource_type,
        }
    )


def _fetch_uos_portal_timetable_from_page(
    page: Any,
    *,
    timezone_name: str,
    timeout_sec: int,
    target_url: str,
    navigate_before_fetch: bool,
    network_samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timeout_ms = max(int(timeout_sec), 1) * 1000
    if navigate_before_fetch:
        page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)
    if not _page_has_timetable_surface(page):
        _navigate_to_timetable_surface(page, timeout_sec=timeout_sec)
    if not _page_has_timetable_surface(page):
        try:
            page.goto(UOS_WISE_ENTRY_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2000)
            if not _page_has_timetable_surface(page) and UOS_PORTAL_LECTURE_SCHEDULE_URL:
                page.goto(
                    UOS_PORTAL_LECTURE_SCHEDULE_URL,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
            page.wait_for_timeout(3000)
            _navigate_to_timetable_surface(page, timeout_sec=timeout_sec)
        except Exception:
            logger.info("uos portal timetable fallback navigation failed", exc_info=True)

    tables = _collect_all_tables(page)
    page_text = _safe_page_text(page)
    current_url = str(getattr(page, "url", "") or "").strip()
    title = _safe_page_title(page)
    has_timetable_surface = _page_has_timetable_surface(page)
    year, semester = extract_year_semester(page_text)
    events = parse_uos_timetable_tables(
        tables,
        timezone_name=timezone_name,
        year=year,
        semester=semester,
    )
    return {
        "ok": bool(events),
        "events": events,
        "target_url": target_url,
        "current_url": current_url,
        "title": title,
        "has_timetable_surface": has_timetable_surface,
        "table_count": len(tables),
        "auth_required": _looks_like_login_failure(
            current_url=current_url,
            title=title,
            page_text=page_text,
            page=page,
        ),
        "network_samples": list(network_samples or [])[:20],
    }


def _sanitize_url_without_query(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value or "").strip()
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _collect_all_tables(page: Any) -> list[list[list[dict[str, Any]]]]:
    tables: list[list[list[dict[str, Any]]]] = []
    frames = [page, *list(getattr(page, "frames", []))]
    for frame in frames:
        try:
            raw_tables = frame.evaluate(
                """
                () => Array.from(document.querySelectorAll('table')).map((table, tableIndex) =>
                  Array.from(table.rows).map((row, rowIndex) =>
                    Array.from(row.cells).map((cell, cellIndex) => ({
                      key: `${tableIndex}:${rowIndex}:${cellIndex}`,
                      text: String(cell.innerText || '').trim(),
                      rowspan: Number(cell.rowSpan || 1),
                      colspan: Number(cell.colSpan || 1),
                    }))
                  )
                )
                """
            )
        except Exception:
            continue
        if isinstance(raw_tables, list):
            for table in raw_tables:
                if isinstance(table, list) and table:
                    tables.append(table)
    return tables


def _expand_table(table: list[list[dict[str, Any]]]) -> list[list[dict[str, Any] | None]]:
    grid: list[list[dict[str, Any] | None]] = []
    for row_index, row in enumerate(table):
        while len(grid) <= row_index:
            grid.append([])
        col_index = 0
        for raw_cell in row:
            if not isinstance(raw_cell, dict):
                continue
            while col_index < len(grid[row_index]) and grid[row_index][col_index] is not None:
                col_index += 1
            cell = {
                "key": str(raw_cell.get("key") or f"{row_index}:{col_index}"),
                "text": str(raw_cell.get("text") or "").strip(),
            }
            rowspan = max(int(raw_cell.get("rowspan") or 1), 1)
            colspan = max(int(raw_cell.get("colspan") or 1), 1)
            for rr in range(row_index, row_index + rowspan):
                while len(grid) <= rr:
                    grid.append([])
                while len(grid[rr]) < col_index + colspan:
                    grid[rr].append(None)
                for cc in range(col_index, col_index + colspan):
                    grid[rr][cc] = cell
            col_index += colspan
    max_width = max((len(row) for row in grid), default=0)
    for row in grid:
        while len(row) < max_width:
            row.append(None)
    return grid


def _find_day_header_row(grid: list[list[dict[str, Any] | None]]) -> int | None:
    for row_index, row in enumerate(grid):
        labels = {
            label
            for cell in row
            if cell is not None
            for label in UOS_DAY_LABELS
            if label == _normalize_text(_cell_text(cell))
        }
        if len(labels) >= 5:
            return row_index
    return None


def _day_column_map(row: list[dict[str, Any] | None]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for col_index, cell in enumerate(row):
        if cell is None:
            continue
        label = _normalize_text(_cell_text(cell))
        weekday = UOS_DAY_LABELS.get(label)
        if weekday:
            mapping[col_index] = weekday
    return mapping


def _find_time_column(
    grid: list[list[dict[str, Any] | None]],
    *,
    day_header_row: int,
) -> int | None:
    counts: dict[int, int] = {}
    for row in grid[day_header_row + 1 :]:
        for col_index, cell in enumerate(row):
            if cell is None:
                continue
            cell_text = _cell_text(cell)
            if TIME_RANGE_RE.search(cell_text) or PERIOD_LABEL_RE.match(cell_text):
                counts[col_index] = counts.get(col_index, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _row_time_ranges(
    grid: list[list[dict[str, Any] | None]],
    *,
    time_column: int,
    day_header_row: int,
) -> dict[int, tuple[str, str]]:
    ranges: dict[int, tuple[str, str]] = {}
    for row_index in range(day_header_row + 1, len(grid)):
        row = grid[row_index]
        if time_column >= len(row):
            continue
        cell = row[time_column]
        if cell is None:
            continue
        cell_text = _cell_text(cell)
        match = TIME_RANGE_RE.search(cell_text)
        if match:
            ranges[row_index] = (match.group("start"), match.group("end"))
            continue
        period_range = _period_label_time_range(cell_text)
        if period_range:
            ranges[row_index] = period_range
    return ranges


def _period_label_time_range(value: str) -> tuple[str, str] | None:
    match = PERIOD_NUMBER_RE.match(str(value or "").strip())
    if not match:
        return None
    try:
        period = int(match.group("period"))
    except (TypeError, ValueError):
        return None
    if period <= 0:
        return None
    start_minutes = (period - 1) * 60
    end_minutes = start_minutes + 50
    start_hour, start_minute = divmod(9 * 60 + start_minutes, 60)
    end_hour, end_minute = divmod(9 * 60 + end_minutes, 60)
    return f"{start_hour:02d}:{start_minute:02d}", f"{end_hour:02d}:{end_minute:02d}"


def _parse_class_cell_text(text: str) -> tuple[str, str | None, str | None, list[str]]:
    lines = [line.strip() for line in re.split(r"[\r\n]+", str(text or "")) if line.strip()]
    if not lines:
        return "", None, None, []
    title = lines[0]
    location = None
    instructor = None
    for line in lines[1:]:
        if location is None:
            match = LOCATION_RE.search(line)
            if match:
                location = f"{match.group('building')}-{match.group('room')}"
                continue
        if instructor is None and not re.search(r"\d{2}:\d{2}", line):
            instructor = line
    if location is None:
        match = LOCATION_RE.search(text)
        if match:
            location = f"{match.group('building')}-{match.group('room')}"
    return title, location, instructor, lines


def _meeting_datetimes(
    *,
    weekday_code: str,
    start_hm: str,
    end_hm: str,
    timezone_name: str,
    current_dt: datetime,
) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    anchor = current_dt.astimezone(tz)
    target_weekday = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"].index(weekday_code)
    day_offset = (target_weekday - anchor.weekday()) % 7
    date_part = (anchor + timedelta(days=day_offset)).date()
    start_hour, start_minute = [int(part) for part in start_hm.split(":", 1)]
    end_hour, end_minute = [int(part) for part in end_hm.split(":", 1)]
    start_dt = datetime(
        date_part.year,
        date_part.month,
        date_part.day,
        start_hour,
        start_minute,
        tzinfo=tz,
    )
    end_dt = datetime(
        date_part.year,
        date_part.month,
        date_part.day,
        end_hour,
        end_minute,
        tzinfo=tz,
    )
    if end_dt <= start_dt:
        end_dt = end_dt + timedelta(days=1)
    return start_dt, end_dt


def _safe_page_title(page: Any) -> str:
    try:
        return str(page.title() or "").strip()
    except Exception:
        return ""


def _safe_page_text(page: Any) -> str:
    snippets: list[str] = []
    try:
        snippets.append(str(page.locator("body").inner_text(timeout=5000) or ""))
    except Exception:
        pass
    for frame in list(getattr(page, "frames", [])):
        try:
            text = str(frame.locator("body").inner_text(timeout=3000) or "")
        except Exception:
            continue
        if text:
            snippets.append(text)
    combined = "\n".join(item for item in snippets if item.strip())
    return combined.strip()


def _page_has_timetable_surface(page: Any) -> bool:
    title = _safe_page_title(page)
    text = _safe_page_text(page)
    return any(label in title or label in text for label in UOS_TIMETABLE_SURFACE_LABELS)


def _click_timetable_navigation(frame: Any) -> bool:
    blocked_labels = [
        re.sub(r"\s+", " ", str(item or "").strip()).lower()
        for item in UOS_TIMETABLE_BLOCKED_LABELS
        if str(item or "").strip()
    ]
    try:
        clicked = frame.evaluate(
            """
            (args) => {
              const targetItemIds = Array.from(args?.target_item_ids || [])
                .map((item) => String(item || '').trim())
                .filter(Boolean);
              const parentItemIds = Array.from(args?.parent_item_ids || [])
                .map((item) => String(item || '').trim())
                .filter(Boolean);
              const labels = Array.from(args?.labels || [])
                .map((item) => String(item || '').trim())
                .filter(Boolean);
              const blockedLabels = Array.from(args?.blocked_labels || [])
                .map((item) => String(item || '').trim().toLowerCase())
                .filter(Boolean);
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const matchesItemId = (value, candidates) => {
                const itemId = String(value || '').trim();
                return candidates.some((candidate) => itemId === candidate || itemId.startsWith(`${candidate}_`));
              };
              const revealAncestors = (el) => {
                for (let node = el; node; node = node.parentElement) {
                  if (node instanceof HTMLElement) {
                    if (node.hidden) node.hidden = false;
                    if (String(node.style?.display || '').trim().toLowerCase() === 'none') {
                      node.style.display = '';
                    }
                  }
                }
              };
              const clickElement = (el) => {
                if (!el) return '';
                revealAncestors(el);
                const target =
                  el.matches?.('a, button, input[type="button"], input[type="submit"]')
                    ? el
                    : el.querySelector?.('a, button, input[type="button"], input[type="submit"]') || el;
                if (!target) return '';
                revealAncestors(target);
                if (typeof target.click === 'function') {
                  target.click();
                }
                return (
                  String(target.getAttribute?.('data-itemid') || '') ||
                  String(target.innerText || target.textContent || target.value || '').trim()
                );
              };
              const menuNodes = Array.from(document.querySelectorAll('[data-itemid]'));
              for (const parentId of parentItemIds) {
                const parent = menuNodes.find((el) => matchesItemId(el.getAttribute('data-itemid'), [parentId]));
                if (parent) {
                  revealAncestors(parent);
                  const parentTarget =
                    parent.matches?.('a, button, input[type="button"], input[type="submit"]')
                      ? parent
                      : parent.querySelector?.('a, button, input[type="button"], input[type="submit"]');
                  if (parentTarget && typeof parentTarget.click === 'function') {
                    parentTarget.click();
                  }
                }
              }
              for (const targetId of targetItemIds) {
                const target = menuNodes.find((el) => matchesItemId(el.getAttribute('data-itemid'), [targetId]));
                const clicked = clickElement(target);
                if (clicked) return clicked;
              }
              const terms = labels.map((item) => normalize(item)).filter(Boolean);
              const elements = Array.from(document.querySelectorAll('a, button, input[type="button"], input[type="submit"], span, td, li, div'));
              for (const el of elements) {
                const text = String(el.innerText || el.textContent || el.value || '').trim();
                if (!text) continue;
                const normalizedText = normalize(text);
                if (blockedLabels.some((item) => normalizedText.includes(item))) continue;
                if (!terms.some((term) => normalizedText === term)) continue;
                const clicked = clickElement(el);
                if (clicked) return clicked;
              }
              return '';
            }
            """,
            {
                "labels": list(UOS_TIMETABLE_NAVIGATION_LABELS),
                "blocked_labels": blocked_labels,
                "target_item_ids": list(UOS_TIMETABLE_MENU_ITEM_IDS),
                "parent_item_ids": list(UOS_TIMETABLE_MENU_PARENT_IDS),
            },
        )
        if bool(str(clicked or "").strip()):
            return True
    except Exception:
        pass
    selectors = []
    for label in UOS_TIMETABLE_NAVIGATION_LABELS:
        selectors.extend(
            [
                f'a:has-text("{label}")',
                f'button:has-text("{label}")',
                f'text="{label}"',
            ]
        )
    for selector in selectors:
        try:
            locator = frame.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _navigate_to_timetable_surface(page: Any, *, timeout_sec: int) -> bool:
    if _page_has_timetable_surface(page):
        return True
    for frame in [page, *list(getattr(page, "frames", []))]:
        if not _click_timetable_navigation(frame):
            continue
        _settle_after_login(page, timeout_sec=timeout_sec)
        if _page_has_timetable_surface(page):
            return True
    return False


def _fill_first_visible(page: Any, selectors: tuple[str, ...], value: str) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.fill(value, timeout=3000)
            return selector
        except Exception:
            continue
    return None


def _click_first_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _settle_after_login(page: Any, *, timeout_sec: int) -> None:
    timeout_ms = max(int(timeout_sec), 1) * 1000
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    page.wait_for_timeout(2500)


def _looks_like_login_failure(
    *,
    current_url: str,
    title: str,
    page_text: str,
    page: Any,
) -> bool:
    current_url_lower = str(current_url or "").strip().lower()
    if UOS_PORTAL_AUTH_REQUIRED_PATH in current_url_lower:
        return True
    text = "\n".join(
        [
            current_url_lower,
            str(title or "").strip().lower(),
            str(page_text or "").strip().lower(),
        ]
    )
    blocked_keywords = [
        "요청하신 페이지가 차단",
        "page is blocked",
        "접근 권한",
        "access denied",
        "permission denied",
    ]
    if any(keyword.lower() in text for keyword in blocked_keywords):
        return True
    success_keywords = [
        "로그아웃",
        "대학행정",
        "today",
        UOS_TIMETABLE_TITLE,
    ]
    if any(keyword.lower() in text for keyword in success_keywords):
        return False
    if "wise.uos.ac.kr" in current_url_lower:
        return False
    has_password_input = False
    for selector in UOS_PORTAL_PASSWORD_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                has_password_input = True
                break
        except Exception:
            continue
    if not has_password_input:
        return False
    failure_keywords = [
        "로그인",
        "login",
        "아이디",
        "비밀번호",
        "password",
        "사용자 정보가 일치하지",
        "입력한 정보",
    ]
    lowered_keywords = [keyword.lower() for keyword in failure_keywords]
    return any(keyword in text for keyword in lowered_keywords)


def _cell_text(cell: dict[str, Any] | None) -> str:
    if not isinstance(cell, dict):
        return ""
    return str(cell.get("text") or "").strip()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _load_playwright_sync_api():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local runtime
        raise RuntimeError(
            "playwright is not installed. Install it with `python -m pip install playwright` "
            "and run `python -m playwright install chromium`."
        ) from exc
    return sync_playwright
