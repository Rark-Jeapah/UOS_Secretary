from __future__ import annotations

import os
from pathlib import Path
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import base64
import queue
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from sidae_secretary.db import parse_metadata_json


DEFAULT_LMS_PROVIDER = "moodle"
PASSWORD_TOKEN_AUTH_MODE = "password_token"
BROWSER_SESSION_AUTH_MODE = "browser_session"
_SAFE_PATH_COMPONENT_RE = re.compile(r"[^0-9A-Za-z._-]+")


def school_directory_provider(entry: dict[str, Any] | None) -> str:
    metadata = parse_metadata_json((entry or {}).get("metadata_json"))
    provider = str(metadata.get("provider") or DEFAULT_LMS_PROVIDER).strip().lower()
    return provider or DEFAULT_LMS_PROVIDER


def school_directory_auth_mode(entry: dict[str, Any] | None) -> str:
    metadata = parse_metadata_json((entry or {}).get("metadata_json"))
    auth_mode = str(metadata.get("auth_mode") or PASSWORD_TOKEN_AUTH_MODE).strip().lower()
    return auth_mode or PASSWORD_TOKEN_AUTH_MODE


def school_directory_login_url(entry: dict[str, Any] | None) -> str:
    item = entry or {}
    for key in ("login_url", "homepage_url", "source_url"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def school_directory_supports_browser_session(entry: dict[str, Any] | None) -> bool:
    return school_directory_auth_mode(entry) == BROWSER_SESSION_AUTH_MODE


def browser_session_profile_dir(
    base_dir: Path,
    *,
    provider: str,
    school_slug: str,
    chat_id: str,
) -> Path:
    resolved_base = Path(base_dir).expanduser().resolve()
    return (
        resolved_base
        / _safe_path_component(provider or DEFAULT_LMS_PROVIDER, default="provider")
        / _safe_path_component(school_slug or "school", default="school")
        / _safe_path_component(chat_id or "chat", default="chat")
    )


def launch_browser_session_login(
    *,
    login_url: str,
    profile_dir: Path,
    browser_channel: str = "",
    browser_executable_path: Path | None = None,
    headless: bool = False,
    timeout_sec: int = 30,
    wait_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    normalized_login_url = str(login_url or "").strip()
    parsed = urlparse(normalized_login_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("학교 로그인 URL이 올바르지 않습니다.")

    sync_playwright = _load_playwright_sync_api()
    resolved_profile_dir = Path(profile_dir).expanduser().resolve()
    resolved_profile_dir = ensure_private_directory(resolved_profile_dir)

    playwright = sync_playwright().start()
    context = None
    page = None
    try:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(resolved_profile_dir),
            "headless": bool(headless),
        }
        executable = Path(browser_executable_path).expanduser().resolve() if browser_executable_path else None
        if executable is not None:
            launch_kwargs["executable_path"] = str(executable)
        else:
            channel = str(browser_channel or "").strip()
            if channel:
                launch_kwargs["channel"] = channel
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            normalized_login_url,
            wait_until="domcontentloaded",
            timeout=max(int(timeout_sec), 1) * 1000,
        )
        if wait_callback is not None:
            wait_callback()
        return {
            "profile_dir": str(resolved_profile_dir),
            "current_url": str(getattr(page, "url", "") or "").strip(),
            "title": _safe_page_title(page),
        }
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass


def _safe_page_title(page: Any) -> str:
    try:
        return str(page.title() or "").strip()
    except Exception:
        return ""


def ensure_private_directory(path: Path, *, mode: int = 0o700) -> Path:
    resolved = Path(path).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(resolved, mode)
    except OSError:
        pass
    return resolved


def sanitize_browser_session_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    item = payload if isinstance(payload, dict) else {}
    output: dict[str, Any] = {}
    current_url = str(item.get("current_url") or "").strip()
    title = str(item.get("title") or "").strip()
    if current_url:
        output["current_url"] = current_url
    if title:
        output["title"] = title
    return output


def _safe_path_component(value: str, *, default: str) -> str:
    text = _SAFE_PATH_COMPONENT_RE.sub("-", str(value or "").strip()).strip(".-")
    return text or default


def _load_playwright_sync_api():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local runtime
        raise RuntimeError(
            "playwright is not installed. Install it with `python -m pip install playwright` "
            "and run `python -m playwright install chromium`."
        ) from exc
    return sync_playwright


class InteractiveBrowserSession:
    def __init__(
        self,
        *,
        login_url: str,
        profile_dir: Path,
        browser_channel: str = "",
        browser_executable_path: Path | None = None,
        headless: bool = True,
        timeout_sec: int = 30,
        viewport_width: int = 900,
        viewport_height: int = 1400,
    ) -> None:
        normalized_login_url = str(login_url or "").strip()
        parsed = urlparse(normalized_login_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("학교 로그인 URL이 올바르지 않습니다.")
        self.login_url = normalized_login_url
        self.profile_dir = ensure_private_directory(Path(profile_dir))
        self.timeout_sec = max(int(timeout_sec), 1)
        self.viewport_width = max(int(viewport_width), 320)
        self.viewport_height = max(int(viewport_height), 480)
        self._activity_lock = threading.Lock()
        self._frame_condition = threading.Condition()
        self._last_activity_at = time.monotonic()
        self._launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": bool(headless),
            "viewport": {
                "width": self.viewport_width,
                "height": self.viewport_height,
            },
        }
        executable = Path(browser_executable_path).expanduser().resolve() if browser_executable_path else None
        if executable is not None:
            self._launch_kwargs["executable_path"] = str(executable)
        else:
            channel = str(browser_channel or "").strip()
            if channel:
                self._launch_kwargs["channel"] = channel
        self._playwright = None
        self._cdp_session = None
        self._context = None
        self._page = None
        self._latest_frame_bytes = b""
        self._latest_frame_seq = 0
        self._latest_frame_content_type = "image/jpeg"
        self._worker_error: Exception | None = None
        self._ready = threading.Event()
        self._closed = False
        self._tasks: queue.Queue[tuple[str, dict[str, Any], Future[Any]]] = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_main,
            name=f"interactive-browser-session-{self.profile_dir.name}",
            daemon=True,
        )
        self._worker.start()
        if not self._ready.wait(timeout=self.timeout_sec + 5):
            raise RuntimeError("원격 브라우저 시작이 시간 내에 완료되지 않았습니다.")
        if self._worker_error is not None:
            raise RuntimeError(str(self._worker_error)) from self._worker_error

    def state(self) -> dict[str, Any]:
        return self._invoke("state")

    def screenshot_png(self) -> bytes:
        return self._invoke("screenshot_png")

    def live_frame(self, *, cursor: int = 0, wait_ms: int = 0) -> tuple[int, bytes, str]:
        target_cursor = max(int(cursor or 0), 0)
        timeout_sec = max(int(wait_ms or 0), 0) / 1000.0
        deadline = time.monotonic() + timeout_sec
        with self._frame_condition:
            while self._latest_frame_seq <= target_cursor and timeout_sec > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._frame_condition.wait(timeout=remaining)
            if self._latest_frame_seq > 0 and self._latest_frame_bytes:
                return (
                    int(self._latest_frame_seq),
                    bytes(self._latest_frame_bytes),
                    str(self._latest_frame_content_type or "image/jpeg"),
                )
        fallback_bytes = self.screenshot_png()
        return (0, fallback_bytes, "image/png")

    def click(self, *, x: float, y: float) -> dict[str, Any]:
        return self._invoke("click", x=float(x), y=float(y))

    def type_text(self, text: str) -> dict[str, Any]:
        value = str(text or "")
        if not value:
            return self.state()
        return self._invoke("type_text", text=value)

    def press(self, key: str) -> dict[str, Any]:
        normalized = str(key or "").strip()
        if not normalized:
            return self.state()
        return self._invoke("press", key=normalized)

    def reload(self) -> dict[str, Any]:
        return self._invoke("reload")

    def cookies(self) -> list[dict[str, Any]]:
        return self._invoke("cookies")

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._invoke("close", timeout_sec=5)
        finally:
            self._closed = True
            if self._worker.is_alive():
                self._worker.join(timeout=5)

    @property
    def last_activity_at(self) -> float:
        with self._activity_lock:
            return self._last_activity_at

    def _touch(self) -> None:
        with self._activity_lock:
            self._last_activity_at = time.monotonic()

    def _invoke(self, operation: str, timeout_sec: int | None = None, **payload: Any) -> Any:
        if self._worker_error is not None:
            raise RuntimeError(str(self._worker_error)) from self._worker_error
        if self._closed and operation != "close":
            raise RuntimeError("원격 브라우저 세션이 이미 종료되었습니다.")
        future: Future[Any] = Future()
        self._tasks.put((operation, payload, future))
        try:
            return future.result(timeout=(timeout_sec or self.timeout_sec + 10))
        except FutureTimeoutError as exc:
            raise RuntimeError("원격 브라우저 응답이 지연되고 있습니다.") from exc

    def _worker_main(self) -> None:
        try:
            sync_playwright = _load_playwright_sync_api()
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(**self._launch_kwargs)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.goto(
                self.login_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_sec * 1000,
            )
            self._page.wait_for_timeout(500)
            self._cdp_session = self._context.new_cdp_session(self._page)
            self._cdp_session.on("Page.screencastFrame", self._handle_screencast_frame)
            self._cdp_session.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 55,
                    "maxWidth": self.viewport_width,
                    "maxHeight": self.viewport_height,
                    "everyNthFrame": 1,
                },
            )
        except Exception as exc:
            self._worker_error = exc
            self._shutdown_worker_resources()
            self._ready.set()
            return

        self._ready.set()
        while True:
            operation, payload, future = self._tasks.get()
            try:
                if operation == "state":
                    result = self._state_impl()
                elif operation == "screenshot_png":
                    result = self._screenshot_png_impl()
                elif operation == "click":
                    result = self._click_impl(x=float(payload.get("x") or 0), y=float(payload.get("y") or 0))
                elif operation == "type_text":
                    result = self._type_text_impl(str(payload.get("text") or ""))
                elif operation == "press":
                    result = self._press_impl(str(payload.get("key") or ""))
                elif operation == "reload":
                    result = self._reload_impl()
                elif operation == "cookies":
                    result = self._cookies_impl()
                elif operation == "close":
                    self._closed = True
                    result = None
                    future.set_result(result)
                    break
                else:
                    raise RuntimeError(f"unsupported browser operation: {operation}")
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
        self._shutdown_worker_resources()

    def _state_impl(self) -> dict[str, Any]:
        self._touch()
        return {
            "profile_dir": str(self.profile_dir),
            "current_url": str(getattr(self._page, "url", "") or "").strip(),
            "title": _safe_page_title(self._page),
            "last_activity_monotonic": self.last_activity_at,
        }

    def _screenshot_png_impl(self) -> bytes:
        self._touch()
        return self._page.screenshot(
            type="png",
            full_page=False,
            animations="disabled",
            caret="hide",
        )

    def _click_impl(self, *, x: float, y: float) -> dict[str, Any]:
        self._touch()
        clamped_x = min(max(float(x), 0.0), float(self.viewport_width - 1))
        clamped_y = min(max(float(y), 0.0), float(self.viewport_height - 1))
        self._page.mouse.click(clamped_x, clamped_y)
        self._page.wait_for_timeout(400)
        return self._state_impl()

    def _type_text_impl(self, text: str) -> dict[str, Any]:
        if not text:
            return self._state_impl()
        self._touch()
        self._page.keyboard.type(text, delay=30)
        self._page.wait_for_timeout(200)
        return self._state_impl()

    def _press_impl(self, key: str) -> dict[str, Any]:
        if not key:
            return self._state_impl()
        self._touch()
        self._page.keyboard.press(key)
        self._page.wait_for_timeout(250)
        return self._state_impl()

    def _reload_impl(self) -> dict[str, Any]:
        self._touch()
        self._page.reload(wait_until="domcontentloaded", timeout=self.timeout_sec * 1000)
        self._page.wait_for_timeout(400)
        return self._state_impl()

    def _cookies_impl(self) -> list[dict[str, Any]]:
        self._touch()
        return list(self._context.cookies())

    def _shutdown_worker_resources(self) -> None:
        if self._cdp_session is not None:
            try:
                self._cdp_session.send("Page.stopScreencast")
            except Exception:
                pass
            self._cdp_session = None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _handle_screencast_frame(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        try:
            frame_bytes = base64.b64decode(str(params.get("data") or ""))
        except Exception:
            frame_bytes = b""
        if frame_bytes:
            with self._frame_condition:
                self._latest_frame_bytes = frame_bytes
                self._latest_frame_seq += 1
                self._latest_frame_content_type = "image/jpeg"
                self._frame_condition.notify_all()
        if self._cdp_session is not None and session_id is not None:
            try:
                self._cdp_session.send("Page.screencastFrameAck", {"sessionId": session_id})
            except Exception:
                pass
