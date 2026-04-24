from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from html import unescape
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as dt_parser
import requests


logger = logging.getLogger(__name__)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LOGIN_TOKEN_RE = re.compile(r'name="logintoken"\s+value="([^"]+)"', re.I)
_COURSE_LINK_RE = re.compile(
    r'<a[^>]+href="([^"]*?/course/view\.php\?id=(\d+)[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_COURSE_SECTION_RE = r"course/view\.php\?id={course_id}&mode=sections&expandsection=(\d+)"
_MATERIAL_LINK_RE = re.compile(
    r'<a[^>]+href="([^"]*?/(?:mod/(?:ubfile|resource)/view\.php\?id=\d+|pluginfile\.php/[^"]+))"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_BOARD_VIEW_RE = re.compile(r"/mod/ubboard/view\.php\?id=(\d+)", re.I)
_BOARD_ARTICLE_RE = re.compile(
    r'<a[^>]+href="([^"]*?/mod/ubboard/article\.php[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_ARTICLE_TITLE_RE = re.compile(
    r'<h3[^>]*class="[^"]*article-title[^"]*"[^>]*>(.*?)</h3>',
    re.I | re.S,
)
_ARTICLE_DATE_RE = re.compile(
    r'<div[^>]*class="[^"]*subject-description-date[^"]*"[^>]*>(.*?)</div>',
    re.I | re.S,
)
_ARTICLE_CONTENT_RE = re.compile(
    r'<div[^>]*class="[^"]*article-content[^"]*"[^>]*>([\s\S]*?)<div[^>]*class="[^"]*article-buttons[^"]*"',
    re.I,
)
_ARTICLE_TEXT_HTML_RE = re.compile(
    r'<div[^>]*class="[^"]*text_to_html[^"]*"[^>]*>([\s\S]*?)</div>',
    re.I,
)
_ARTICLE_FILE_ITEM_RE = re.compile(
    r'<li[^>]*class="[^"]*file-item[^"]*"[^>]*>[\s\S]*?'
    r'<a[^>]+href="([^"]*?/pluginfile\.php/[^"]+)"[^>]*>[\s\S]*?'
    r'<div[^>]*class="[^"]*file-name[^"]*"[^>]*>(.*?)</div>',
    re.I,
)


def _html_to_text(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = _HTML_TAG_RE.sub(" ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _uclass_origin(ws_base_url: str) -> str:
    parsed = urlparse(str(ws_base_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("invalid UCLASS_WS_BASE URL")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _extract_login_token(page_html: str) -> str:
    match = _LOGIN_TOKEN_RE.search(page_html)
    if match:
        return match.group(1)
    alt = re.search(r'"logintoken"\s*:\s*"([^"]+)"', page_html, re.I)
    return alt.group(1) if alt else ""


def login_uclass_session(
    *,
    ws_base_url: str,
    username: str,
    password: str,
    timeout_sec: int = 30,
    session: requests.Session | None = None,
) -> requests.Session:
    user = str(username or "").strip()
    secret = str(password or "")
    if not user:
        raise ValueError("UCLASS username is required")
    if not secret:
        raise ValueError("UCLASS password is required")
    origin = _uclass_origin(ws_base_url)
    client = session or requests.Session()
    client.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    login_page = client.get(
        f"{origin}/login/index.php",
        timeout=timeout_sec,
        allow_redirects=True,
    )
    login_page.raise_for_status()
    payload = {
        "anchor": "",
        "username": user,
        "password": secret,
        "rememberusername": 1,
    }
    login_token = _extract_login_token(login_page.text)
    if login_token:
        payload["logintoken"] = login_token
    response = client.post(
        f"{origin}/login/index.php",
        data=payload,
        timeout=timeout_sec,
        allow_redirects=True,
    )
    response.raise_for_status()
    dashboard = client.get(
        f"{origin}/my/courses.php",
        timeout=timeout_sec,
        allow_redirects=True,
    )
    dashboard.raise_for_status()
    if "/login/index.php" in str(dashboard.url):
        raise RuntimeError("uclass session login failed")
    return client


def _extract_dashboard_courses(page_html: str, origin: str) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for href, raw_course_id, inner in _COURSE_LINK_RE.findall(page_html):
        try:
            course_id = int(raw_course_id)
        except (TypeError, ValueError):
            continue
        name = _html_to_text(inner)
        if len(name) < 2:
            continue
        seen[course_id] = {
            "id": course_id,
            "name": name,
            "url": urljoin(origin, unescape(href)),
        }
    return [seen[key] for key in sorted(seen)]


def _extract_course_section_ids(page_html: str, course_id: int) -> list[int]:
    pattern = re.compile(_COURSE_SECTION_RE.format(course_id=int(course_id)), re.I)
    values: set[int] = set()
    for raw_value in pattern.findall(page_html):
        try:
            values.add(int(raw_value))
        except (TypeError, ValueError):
            continue
    return sorted(values)


def _dedupe_text_urls(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for text, url in rows:
        item = (text, url)
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _extract_session_material_links(page_html: str, origin: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for href, inner in _MATERIAL_LINK_RE.findall(page_html):
        text = _html_to_text(inner)
        absolute = urljoin(origin, unescape(href))
        if not absolute:
            continue
        if not text:
            text = _filename_from_url(absolute, default="material")
        links.append((text, absolute))
    return _dedupe_text_urls(links)


def _extract_board_ids(page_html: str) -> list[int]:
    values: set[int] = set()
    for raw_value in _BOARD_VIEW_RE.findall(page_html):
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            continue
        if parsed <= 0:
            continue
        values.add(parsed)
    return sorted(values)


def _extract_board_article_links(page_html: str, origin: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for href, _ in _BOARD_ARTICLE_RE.findall(page_html):
        absolute = urljoin(origin, unescape(href))
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
    return links


def _date_folder_from_text(value: str | None, fallback: str) -> str:
    parsed = _to_iso_guess(value)
    if not parsed:
        return fallback
    return parsed[:10]


def _default_material_date_folder(
    *,
    timezone_name: str = "Asia/Seoul",
    current_dt: datetime | None = None,
) -> str:
    tz_name = str(timezone_name or "Asia/Seoul").strip() or "Asia/Seoul"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    if current_dt is None:
        anchor = datetime.now(tz)
    elif current_dt.tzinfo is None:
        anchor = current_dt.replace(tzinfo=tz)
    else:
        anchor = current_dt.astimezone(tz)
    return anchor.strftime("%Y-%m-%d")


def _article_context(page_html: str) -> tuple[str, str | None, str]:
    title_match = _ARTICLE_TITLE_RE.search(page_html)
    date_match = _ARTICLE_DATE_RE.search(page_html)
    content_match = _ARTICLE_CONTENT_RE.search(page_html)
    title = _html_to_text(title_match.group(1)) if title_match else "material"
    date_text = _html_to_text(date_match.group(1)) if date_match else None
    if content_match:
        content_html = content_match.group(1)
        text_html_match = _ARTICLE_TEXT_HTML_RE.search(content_html)
        body = _html_to_text(text_html_match.group(1)) if text_html_match else _html_to_text(content_html)
    else:
        body = ""
    return title, date_text, body


def _extract_article_file_links(page_html: str, origin: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for href, filename_html in _ARTICLE_FILE_ITEM_RE.findall(page_html):
        absolute = urljoin(origin, unescape(href))
        filename = _html_to_text(filename_html)
        if not filename:
            filename = _filename_from_url(absolute, default="material")
        links.append((filename, absolute))
    return _dedupe_text_urls(links)


def _material_candidate_from_session_link(
    *,
    course_id: int,
    course_name: str,
    display_name: str,
    url: str,
    date_folder: str,
    source_kind: str,
    metadata: dict[str, Any] | None = None,
) -> MaterialCandidate:
    seed = f"{course_id}|{source_kind}|{display_name}|{url}"
    course_key = course_slug(course_name, fallback=f"course_{course_id}")
    payload = {
        "course_id": course_id,
        "course_name": course_name,
        "source_kind": source_kind,
        "context_text": display_name,
    }
    if metadata:
        payload.update(metadata)
    return MaterialCandidate(
        external_id=_material_external_id(seed),
        filename=display_name,
        url=url,
        course=course_key,
        date_folder=date_folder,
        metadata=payload,
    )


def _should_include_module_url_as_material(module: dict[str, Any], module_url: str) -> bool:
    modname = str(module.get("modname") or "").strip().lower()
    parsed = urlparse(str(module_url or "").strip())
    path = str(parsed.path or "").strip().lower()
    # Generic Moodle module view.php endpoints are usually container pages that
    # redirect to login/error HTML instead of the downloadable file bytes.
    if path.endswith("/view.php"):
        return False
    return modname != "ubboard"


def scrape_material_candidates_via_session(
    *,
    ws_base_url: str,
    username: str,
    password: str,
    timezone_name: str = "Asia/Seoul",
    current_dt: datetime | None = None,
    timeout_sec: int = 30,
    session: requests.Session | None = None,
    max_sections_per_course: int = 8,
    max_boards_per_course: int = 8,
    max_articles_per_board: int = 6,
) -> list[MaterialCandidate]:
    origin = _uclass_origin(ws_base_url)
    client = login_uclass_session(
        ws_base_url=ws_base_url,
        username=username,
        password=password,
        timeout_sec=timeout_sec,
        session=session,
    )
    dashboard = client.get(
        f"{origin}/my/courses.php",
        timeout=timeout_sec,
        allow_redirects=True,
    )
    dashboard.raise_for_status()
    today = _default_material_date_folder(
        timezone_name=timezone_name,
        current_dt=current_dt,
    )
    candidates: dict[str, MaterialCandidate] = {}

    for course in _extract_dashboard_courses(dashboard.text, origin):
        course_id = int(course["id"])
        course_name = str(course["name"])
        course_page = client.get(str(course["url"]), timeout=timeout_sec, allow_redirects=True)
        course_page.raise_for_status()
        course_pages = [course_page.text]
        for section_id in _extract_course_section_ids(course_page.text, course_id)[:max_sections_per_course]:
            section_url = (
                f"{origin}/course/view.php?id={course_id}"
                f"&mode=sections&expandsection={section_id}#section-{section_id}"
            )
            section_page = client.get(section_url, timeout=timeout_sec, allow_redirects=True)
            section_page.raise_for_status()
            course_pages.append(section_page.text)

        article_urls: list[str] = []
        board_ids: set[int] = set()
        for page_html in course_pages:
            for display_name, url in _extract_session_material_links(page_html, origin):
                candidate = _material_candidate_from_session_link(
                    course_id=course_id,
                    course_name=course_name,
                    display_name=display_name,
                    url=url,
                    date_folder=today,
                    source_kind="course_page_link",
                )
                candidates[candidate.external_id] = candidate
            board_ids.update(_extract_board_ids(page_html))
            article_urls.extend(_extract_board_article_links(page_html, origin))

        for board_id in sorted(board_ids)[:max_boards_per_course]:
            try:
                board_page = client.get(
                    f"{origin}/mod/ubboard/view.php?id={board_id}",
                    timeout=timeout_sec,
                    allow_redirects=True,
                )
                board_page.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "uclass board scrape failed",
                    extra={"board_id": board_id, "error": str(exc)},
                )
                continue
            article_urls.extend(
                _extract_board_article_links(board_page.text, origin)[:max_articles_per_board]
            )

        seen_articles: set[str] = set()
        deduped_article_urls: list[str] = []
        for article_url in article_urls:
            if article_url in seen_articles:
                continue
            seen_articles.add(article_url)
            deduped_article_urls.append(article_url)

        for article_url in deduped_article_urls[: max_boards_per_course * max_articles_per_board]:
            try:
                article_page = client.get(article_url, timeout=timeout_sec, allow_redirects=True)
                article_page.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "uclass article scrape failed",
                    extra={"url": article_url, "error": str(exc)},
                )
                continue
            article_title, article_date, article_body = _article_context(article_page.text)
            date_folder = _date_folder_from_text(article_date, fallback=today)
            context_text = "\n".join(
                [item for item in [article_title, article_body] if item]
            ).strip()
            for filename, file_url in _extract_article_file_links(article_page.text, origin):
                candidate = _material_candidate_from_session_link(
                    course_id=course_id,
                    course_name=course_name,
                    display_name=filename,
                    url=file_url,
                    date_folder=date_folder,
                    source_kind="ubboard_attachment",
                    metadata={
                        "article_title": article_title,
                        "article_date": article_date,
                        "article_url": article_url,
                        "article_body": article_body,
                        "context_text": context_text or filename,
                    },
                )
                candidates[candidate.external_id] = candidate

    return list(candidates.values())


def _epoch_to_iso(epoch: int | float | str | None) -> str | None:
    if epoch is None:
        return None
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return None
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _to_iso_guess(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _epoch_to_iso(value)
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.isdigit():
            return _epoch_to_iso(candidate)
        try:
            parsed = dt_parser.isoparse(candidate)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return None


def _stable_external_id(prefix: str, raw_id: Any, fallback_seed: str) -> str:
    if raw_id not in (None, "", 0, "0"):
        return f"{prefix}:{raw_id}"
    digest = sha1(fallback_seed.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _filename_from_url(url: str | None, default: str) -> str:
    if not url:
        return default
    parsed = urlparse(url)
    tail = Path(parsed.path).name
    return tail or default


def infer_moodle_token_endpoint(ws_base_url: str) -> str:
    parsed = urlparse(str(ws_base_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("invalid UCLASS_WS_BASE URL")
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "/login/token.php",
            "",
            "",
            "",
        )
    )


def request_moodle_ws_token(
    *,
    ws_base_url: str,
    username: str,
    password: str,
    service: str = "moodle_mobile_app",
    token_endpoint: str | None = None,
    timeout_sec: int = 30,
) -> str:
    endpoint = str(token_endpoint or "").strip() or infer_moodle_token_endpoint(ws_base_url)
    user = str(username or "").strip()
    secret = str(password or "")
    if not user:
        raise ValueError("UCLASS username is required")
    if not secret:
        raise ValueError("UCLASS password is required")
    payload = {
        "username": user,
        "password": secret,
        "service": str(service or "moodle_mobile_app").strip() or "moodle_mobile_app",
    }
    response = requests.post(endpoint, data=payload, timeout=timeout_sec)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("uclass token endpoint returned invalid response")
    token = str(body.get("token") or "").strip()
    if token:
        return token
    if body.get("error") or body.get("errorcode") or body.get("exception"):
        code = str(body.get("errorcode") or body.get("error") or "unknown")
        message = str(body.get("message") or body.get("debuginfo") or "").strip()
        if message:
            raise RuntimeError(f"uclass token error {code}: {message}")
        raise RuntimeError(f"uclass token error {code}")
    raise RuntimeError("uclass token endpoint did not return token")


def _mobile_launch_candidate_tokens(location: str) -> list[str]:
    parsed = urlparse(str(location or "").strip())
    raw_netloc = str(parsed.netloc or "")
    if parsed.scheme != "moodlemobile" or not raw_netloc.startswith("token="):
        return []
    encoded = raw_netloc.split("token=", 1)[1].strip()
    if not encoded:
        return []
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return []
    parts = [part.strip() for part in decoded.split(":::") if part.strip()]
    ordered: list[str] = []
    if len(parts) >= 2:
        ordered.extend([parts[1], parts[0]])
        ordered.extend(parts[2:])
    else:
        ordered.extend(parts)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _candidate_ws_token_is_valid(
    *,
    ws_base_url: str,
    token: str,
    request_method: str = "GET",
    timeout_sec: int = 30,
    validation_wsfunction: str = "core_webservice_get_site_info",
) -> bool:
    payload = {
        "wstoken": str(token or "").strip(),
        "moodlewsrestformat": "json",
        "wsfunction": str(validation_wsfunction or "core_webservice_get_site_info").strip(),
    }
    if request_method.upper().strip() == "POST":
        response = requests.post(ws_base_url, data=payload, timeout=timeout_sec)
    else:
        response = requests.get(ws_base_url, params=payload, timeout=timeout_sec)
    response.raise_for_status()
    body = response.json()
    return not (isinstance(body, dict) and body.get("exception"))


def request_moodle_mobile_launch_token(
    *,
    ws_base_url: str,
    username: str,
    password: str,
    service: str = "moodle_mobile_app",
    timeout_sec: int = 30,
    session: requests.Session | None = None,
    request_method: str = "GET",
    validation_wsfunction: str = "core_webservice_get_site_info",
) -> str:
    origin = _uclass_origin(ws_base_url)
    client = session or login_uclass_session(
        ws_base_url=ws_base_url,
        username=username,
        password=password,
        timeout_sec=timeout_sec,
    )
    launch_response = client.get(
        f"{origin}/admin/tool/mobile/launch.php",
        params={
            "service": str(service or "moodle_mobile_app").strip() or "moodle_mobile_app",
            "passport": "sidae-secretary",
            "urlscheme": "moodlemobile",
        },
        timeout=timeout_sec,
        allow_redirects=False,
    )
    launch_response.raise_for_status()
    location = str(launch_response.headers.get("location") or "").strip()
    candidates = _mobile_launch_candidate_tokens(location)
    if not candidates:
        raise RuntimeError("uclass mobile launch did not return token candidates")
    for candidate in candidates:
        if _candidate_ws_token_is_valid(
            ws_base_url=ws_base_url,
            token=candidate,
            request_method=request_method,
            timeout_sec=timeout_sec,
            validation_wsfunction=validation_wsfunction,
        ):
            return candidate
    raise RuntimeError("uclass mobile launch returned unusable token candidates")


@dataclass
class NormalizedNotification:
    external_id: str
    created_at: str
    title: str
    body: str | None
    url: str | None
    metadata: dict[str, Any]


@dataclass
class NormalizedTask:
    external_id: str
    due_at: str | None
    title: str
    status: str
    metadata: dict[str, Any]


@dataclass
class NormalizedEvent:
    external_id: str
    start_at: str
    end_at: str
    title: str
    location: str | None
    rrule: str | None
    metadata: dict[str, Any]


@dataclass
class MaterialCandidate:
    external_id: str
    filename: str
    url: str | None
    course: str
    date_folder: str
    metadata: dict[str, Any]


class MoodleWSClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        request_method: str = "GET",
        timeout_sec: int = 30,
    ):
        self.base_url = base_url
        self.token = token
        self.request_method = request_method.upper().strip()
        self.timeout_sec = timeout_sec
        self.site_userid: int | None = None

    def call(self, wsfunction: str, params: dict[str, Any] | None = None) -> Any:
        payload = {
            "wstoken": self.token,
            "moodlewsrestformat": "json",
            "wsfunction": wsfunction,
        }
        if params:
            payload.update(params)

        logger.info("calling uclass ws", extra={"wsfunction": wsfunction})
        method = self.request_method
        if method == "POST":
            response = requests.post(
                self.base_url, data=payload, timeout=self.timeout_sec
            )
        else:
            response = requests.get(
                self.base_url, params=payload, timeout=self.timeout_sec
            )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("exception"):
            raise RuntimeError(
                f"moodle ws error {data.get('errorcode')}: {data.get('message')}"
            )
        return data

    def get_site_info(self, wsfunction: str) -> dict[str, Any]:
        data = self.call(wsfunction, params={})
        if not isinstance(data, dict):
            return {"raw": data}
        userid = data.get("userid")
        try:
            self.site_userid = int(userid) if userid is not None else None
        except (TypeError, ValueError):
            self.site_userid = None
        return data

    def get_popup_notifications(self, wsfunction: str, limit: int = 50) -> Any:
        params: dict[str, Any] = {
            "newestfirst": 1,
            "limit": int(limit),
            "offset": 0,
        }
        if self.site_userid is not None:
            params["useridto"] = int(self.site_userid)
        return self.call(wsfunction, params=params)

    def get_action_events(
        self,
        wsfunction: str,
        timesortfrom: int | None = None,
        limitnum: int = 50,
    ) -> Any:
        params: dict[str, Any] = {"limitnum": limitnum}
        if timesortfrom is not None:
            params["timesortfrom"] = timesortfrom
        return self.call(wsfunction, params=params)

    def get_users_courses(self, wsfunction: str) -> Any:
        params: dict[str, Any] = {}
        if self.site_userid is not None:
            params["userid"] = int(self.site_userid)
        return self.call(wsfunction, params=params)

    def get_course_contents(self, wsfunction: str, course_id: int) -> Any:
        return self.call(wsfunction, params={"courseid": int(course_id)})

    def get_assignments(self, wsfunction: str, course_ids: list[int]) -> Any:
        params: dict[str, Any] = {}
        for idx, course_id in enumerate(course_ids):
            params[f"courseids[{idx}]"] = int(course_id)
        return self.call(wsfunction, params=params)

    def get_forums(self, wsfunction: str, course_ids: list[int]) -> Any:
        params: dict[str, Any] = {}
        for idx, course_id in enumerate(course_ids):
            params[f"courseids[{idx}]"] = int(course_id)
        return self.call(wsfunction, params=params)

    def get_forum_discussions(
        self,
        wsfunction: str,
        forum_id: int,
        page: int = 0,
        per_page: int = 20,
    ) -> Any:
        return self.call(
            wsfunction,
            params={
                "forumid": int(forum_id),
                "page": int(page),
                "perpage": int(per_page),
            },
        )


def extract_course_index(payload: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(payload, list):
        return {}
    index: dict[int, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        course_id = item.get("id")
        if course_id is None:
            continue
        try:
            cid = int(course_id)
        except (TypeError, ValueError):
            continue
        index[cid] = item
    return index


def course_slug(course_name: str | None, fallback: str = "general") -> str:
    raw = str(course_name or "").strip()
    if not raw:
        raw = fallback
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_") or fallback


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _course_context(
    raw: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    fallback = fallback if isinstance(fallback, dict) else {}
    course_id = None
    for key in ("courseid", "course_id", "course"):
        candidate = _int_or_none(raw.get(key))
        if candidate is not None:
            course_id = candidate
            break
    if course_id is None:
        for key in ("courseid", "course_id", "course"):
            candidate = _int_or_none(fallback.get(key))
            if candidate is not None:
                course_id = candidate
                break
    if course_id is not None:
        context["course_id"] = course_id

    course_name = ""
    for key in (
        "coursefullname",
        "course_name",
        "coursename",
        "course",
        "fullname",
        "displayname",
        "shortname",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            course_name = value.strip()
            break
    if not course_name:
        for key in (
            "coursefullname",
            "course_name",
            "coursename",
            "fullname",
            "displayname",
            "shortname",
        ):
            value = fallback.get(key)
            if isinstance(value, str) and value.strip():
                course_name = value.strip()
                break
    if course_name:
        context["course_name"] = course_name
    return context


def normalize_notifications(payload: Any) -> list[NormalizedNotification]:
    if isinstance(payload, dict):
        raw_items = (
            payload.get("notifications")
            or payload.get("items")
            or payload.get("messages")
            or []
        )
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    normalized: list[NormalizedNotification] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        body = (
            item.get("fullmessage")
            or item.get("smallmessage")
            or item.get("message")
            or item.get("text")
            or ""
        )
        title = (
            item.get("subject")
            or item.get("title")
            or item.get("name")
            or (str(body).strip()[:80] or "Notification")
        )
        created_at = (
            _to_iso_guess(item.get("timecreated"))
            or _to_iso_guess(item.get("createdat"))
            or _to_iso_guess(item.get("timemodified"))
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        url = item.get("contexturl") or item.get("url") or item.get("itemurl")
        seed = f"{title}|{created_at}|{body}|{url}"
        external_id = _stable_external_id(
            "uclass:notif",
            item.get("id") or item.get("notificationid"),
            seed,
        )
        normalized.append(
            NormalizedNotification(
                external_id=external_id,
                created_at=created_at,
                title=str(title),
                body=str(body) if body else None,
                url=str(url) if url else None,
                metadata={"raw": item, **_course_context(item)},
            )
        )
    return normalized


def normalize_action_events(payload: Any) -> tuple[list[NormalizedTask], list[NormalizedEvent]]:
    if isinstance(payload, dict):
        raw_items = payload.get("events") or payload.get("items") or []
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    tasks: list[NormalizedTask] = []
    events: list[NormalizedEvent] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = (
            item.get("name")
            or item.get("title")
            or item.get("eventname")
            or "Untitled"
        )
        due_at = (
            _to_iso_guess(item.get("timesort"))
            or _to_iso_guess(item.get("timedue"))
            or _to_iso_guess(item.get("timeend"))
            or _to_iso_guess(item.get("timestart"))
        )
        completed = item.get("completed")
        status = "completed" if bool(completed) else "pending"
        external_id = _stable_external_id(
            "uclass:task",
            item.get("id") or item.get("eventid"),
            f"{title}|{due_at}|{status}",
        )
        tasks.append(
            NormalizedTask(
                external_id=external_id,
                due_at=due_at,
                title=str(title),
                status=status,
                metadata={"raw": item, **_course_context(item)},
            )
        )

        start_at = _to_iso_guess(item.get("timestart"))
        end_at = _to_iso_guess(item.get("timeend"))
        if start_at and end_at:
            events.append(
                NormalizedEvent(
                    external_id=_stable_external_id(
                        "uclass:event",
                        item.get("id") or item.get("eventid"),
                        f"{title}|{start_at}|{end_at}",
                    ),
                    start_at=start_at,
                    end_at=end_at,
                    title=str(title),
                    location=item.get("location"),
                    rrule=None,
                    metadata={"raw": item, **_course_context(item)},
                )
            )
    return tasks, events


def normalize_assignments(
    payload: Any,
    course_index: dict[int, dict[str, Any]] | None = None,
) -> list[NormalizedTask]:
    if not isinstance(payload, dict):
        return []
    courses = payload.get("courses")
    if not isinstance(courses, list):
        return []

    normalized: list[NormalizedTask] = []
    for course in courses:
        if not isinstance(course, dict):
            continue
        assignments = course.get("assignments")
        if not isinstance(assignments, list):
            continue
        course_id = course.get("id")
        course_meta = {}
        if course_index and course_id is not None:
            try:
                course_meta = course_index.get(int(course_id), {})
            except (TypeError, ValueError):
                course_meta = {}
        course_name = (
            course.get("fullname")
            or course_meta.get("fullname")
            or course_meta.get("shortname")
            or f"course-{course_id}"
        )
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            title = str(assignment.get("name") or "Assignment")
            due_at = (
                _to_iso_guess(assignment.get("duedate"))
                or _to_iso_guess(assignment.get("cutoffdate"))
                or _to_iso_guess(assignment.get("allowsubmissionsfromdate"))
            )
            external_id = _stable_external_id(
                "uclass:assign",
                assignment.get("id"),
                f"{course_id}|{title}|{due_at}",
            )
            status = "pending"
            if assignment.get("alwaysshowdescription"):
                status = "open"
            normalized.append(
                NormalizedTask(
                    external_id=external_id,
                    due_at=due_at,
                    title=title,
                    status=status,
                    metadata={
                        "course_id": course_id,
                        "course_name": course_name,
                        "raw": assignment,
                    },
                )
            )
    return normalized


def _material_external_id(seed: str) -> str:
    return f"uclass:artifact:{sha1(seed.encode('utf-8')).hexdigest()[:24]}"


def extract_material_candidates_from_course_contents(
    course_contents: dict[int, Any],
    course_index: dict[int, dict[str, Any]] | None = None,
    *,
    timezone_name: str = "Asia/Seoul",
    current_dt: datetime | None = None,
) -> list[MaterialCandidate]:
    today = _default_material_date_folder(
        timezone_name=timezone_name,
        current_dt=current_dt,
    )
    candidates: dict[str, MaterialCandidate] = {}
    course_index = course_index or {}

    for course_id, payload in course_contents.items():
        sections = payload if isinstance(payload, list) else []
        course_meta = course_index.get(course_id, {})
        course_name = (
            course_meta.get("fullname")
            or course_meta.get("shortname")
            or f"course-{course_id}"
        )
        course_key = course_slug(str(course_name), fallback=f"course_{course_id}")
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_name = str(section.get("name") or "").strip()
            modules = section.get("modules")
            if not isinstance(modules, list):
                continue
            for module in modules:
                if not isinstance(module, dict):
                    continue
                module_id = module.get("id")
                module_name = str(module.get("name") or "material")
                urls: list[tuple[str, str]] = []
                module_url = module.get("url")
                if (
                    isinstance(module_url, str)
                    and module_url.startswith("http")
                    and _should_include_module_url_as_material(module, module_url)
                ):
                    filename = _filename_from_url(module_url, default=f"{module_name}.html")
                    urls.append((module_url, filename))

                contents = module.get("contents")
                if isinstance(contents, list):
                    for content in contents:
                        if not isinstance(content, dict):
                            continue
                        file_url = content.get("fileurl") or content.get("url")
                        if not isinstance(file_url, str) or not file_url.startswith("http"):
                            continue
                        filename = str(content.get("filename") or "").strip()
                        if not filename:
                            filename = _filename_from_url(file_url, default=module_name)
                        urls.append((file_url, filename))

                for url, filename in urls:
                    seed = f"{course_id}|{module_id}|{filename}|{url}"
                    external_id = _material_external_id(seed)
                    candidates[external_id] = MaterialCandidate(
                        external_id=external_id,
                        filename=filename,
                        url=url,
                        course=course_key,
                        date_folder=today,
                        metadata={
                            "course_id": course_id,
                            "course_name": course_name,
                            "section_name": section_name,
                            "module_id": module_id,
                            "module_name": module_name,
                            "module_type": module.get("modname"),
                            "raw": module,
                        },
                    )
    return list(candidates.values())


def normalize_forum_notifications(
    payload: Any,
    forum: dict[str, Any] | None = None,
) -> list[NormalizedNotification]:
    if not isinstance(payload, dict):
        return []
    discussions = payload.get("discussions")
    if not isinstance(discussions, list):
        return []
    forum = forum or {}
    forum_name = str(forum.get("name") or "Forum")
    forum_id = forum.get("id")

    normalized: list[NormalizedNotification] = []
    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        created_at = (
            _to_iso_guess(discussion.get("timemodified"))
            or _to_iso_guess(discussion.get("created"))
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        title = str(
            discussion.get("name")
            or discussion.get("subject")
            or f"{forum_name} discussion"
        )
        body = discussion.get("message")
        url = discussion.get("discussionurl") or discussion.get("url")
        external_id = _stable_external_id(
            "uclass:forum",
            discussion.get("discussion"),
            f"{forum_id}|{title}|{created_at}",
        )
        normalized.append(
            NormalizedNotification(
                external_id=external_id,
                created_at=created_at,
                title=title,
                body=str(body) if body else None,
                url=str(url) if url else None,
                metadata={"forum": forum, "raw": discussion, **_course_context(discussion, fallback=forum)},
            )
        )
    return normalized


def extract_material_candidates(
    notifications: list[NormalizedNotification],
    tasks: list[NormalizedTask],
    events: list[NormalizedEvent],
    *,
    timezone_name: str = "Asia/Seoul",
    current_dt: datetime | None = None,
) -> list[MaterialCandidate]:
    candidates: list[MaterialCandidate] = []
    raw_dicts: list[dict[str, Any]] = []
    date_folder = _default_material_date_folder(
        timezone_name=timezone_name,
        current_dt=current_dt,
    )
    for item in notifications:
        raw = item.metadata.get("raw")
        if isinstance(raw, dict):
            raw_dicts.append(raw)
    for item in tasks:
        raw = item.metadata.get("raw")
        if isinstance(raw, dict):
            raw_dicts.append(raw)
    for item in events:
        raw = item.metadata.get("raw")
        if isinstance(raw, dict):
            raw_dicts.append(raw)

    for raw in raw_dicts:
        urls: list[str] = []
        for key in ("url", "contexturl", "fileurl", "downloadurl", "itemurl"):
            value = raw.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
        attachments = raw.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                url = (
                    attachment.get("fileurl")
                    or attachment.get("url")
                    or attachment.get("downloadurl")
                )
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)

        if not urls:
            continue
        course_context = _course_context(raw)
        course = str(course_context.get("course_name") or "general")
        course_slug_value = course_slug(course)
        for url in urls:
            filename = _filename_from_url(url, default="material.bin")
            seed = f"{course_slug_value}|{filename}|{url}"
            external_id = _material_external_id(seed)
            candidates.append(
                MaterialCandidate(
                    external_id=external_id,
                    filename=filename,
                    url=url,
                    course=course_slug_value,
                    date_folder=date_folder,
                    metadata={"raw": raw, "url": url, **course_context},
                )
            )
    deduped: dict[str, MaterialCandidate] = {item.external_id: item for item in candidates}
    return list(deduped.values())
