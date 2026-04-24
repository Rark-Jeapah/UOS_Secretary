from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import urlencode


def render_html_page(title: str, body_html: str) -> bytes:
    page = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4efe6;
      --card: #fffaf1;
      --line: #d4c7b3;
      --text: #1d1a16;
      --muted: #6d665d;
      --accent: #14532d;
      --accent-2: #d97706;
      --danger: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(217, 119, 6, 0.10), transparent 28rem),
        linear-gradient(180deg, #f7f1e7 0%, var(--bg) 100%);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
    }}
    .card {{
      width: 100%;
      max-width: 34rem;
      background: rgba(255, 250, 241, 0.96);
      border: 1px solid var(--line);
      border-radius: 1.25rem;
      box-shadow: 0 1.5rem 4rem rgba(64, 49, 30, 0.12);
      padding: 1.5rem;
    }}
    h1 {{
      margin: 0 0 0.75rem;
      font-size: 1.4rem;
    }}
    p, li {{
      color: var(--muted);
      line-height: 1.55;
    }}
    label {{
      display: block;
      margin: 1rem 0 0.35rem;
      font-weight: 700;
      color: var(--text);
    }}
    .field-hint {{
      margin: 0.35rem 0 0;
      font-size: 0.92rem;
      color: var(--muted);
      line-height: 1.5;
    }}
    input, select {{
      width: 100%;
      padding: 0.85rem 0.9rem;
      border: 1px solid var(--line);
      border-radius: 0.8rem;
      font-size: 1rem;
      background: #fff;
    }}
    button {{
      width: 100%;
      margin-top: 1.25rem;
      border: 0;
      border-radius: 999px;
      padding: 0.9rem 1rem;
      font-size: 1rem;
      font-weight: 700;
      color: #fff;
      background: linear-gradient(135deg, var(--accent), #166534);
    }}
    .error {{
      margin-top: 1rem;
      padding: 0.85rem 0.95rem;
      border-radius: 0.8rem;
      background: rgba(185, 28, 28, 0.08);
      color: var(--danger);
      border: 1px solid rgba(185, 28, 28, 0.16);
    }}
    .note {{
      margin-top: 1rem;
      padding-left: 1rem;
    }}
    .success {{
      color: var(--accent);
      font-weight: 700;
    }}
  </style>
</head>
<body>
  <main class="card">
    {body_html}
  </main>
</body>
</html>
"""
    return page.encode("utf-8")


def _resolved_school_note_html(
    *,
    resolved_school: dict[str, Any] | None,
    resolved_portal_info: dict[str, Any] | None,
    resolved_support: dict[str, Any] | None,
    resolved_is_uos_school: bool,
) -> str:
    if not resolved_school:
        return ""
    if resolved_is_uos_school:
        return (
            "<p><strong>선택된 학교:</strong> 서울시립대학교 학교 계정으로 "
            "온라인강의실을 연결하고 시간표는 학교 공식 API로 자동 동기화합니다.</p>"
        )
    if resolved_portal_info:
        constraint_text = str(resolved_portal_info.get("constraints") or "").strip()
        support = resolved_support if isinstance(resolved_support, dict) else {}
        resolved_note = (
            f'<p><strong>선택된 학교:</strong> {html.escape(str(resolved_school.get("display_name") or ""))} '
            "온라인강의실 연결을 확인합니다.</p>"
        )
        resolved_note += (
            f'<p class="field-hint">{html.escape(str(resolved_portal_info.get("display_name") or "학교 포털"))}는 '
            "같은 학교 계정을 쓰는 것으로 등록돼 있습니다. "
            "포털 자동 연동 범위는 학교별로 다를 수 있습니다.</p>"
        )
        if constraint_text:
            resolved_note += (
                f'<p class="field-hint"><strong>현재 제약:</strong> {html.escape(constraint_text)}</p>'
            )
        if not bool(support.get("official_user_support")):
            resolved_note += (
                '<p class="field-hint"><strong>안내:</strong> 현재 사용자-facing 공식 지원 학교는 서울시립대학교입니다.</p>'
            )
        return resolved_note
    return (
        f'<p><strong>선택된 학교:</strong> {html.escape(str(resolved_school.get("display_name") or ""))}'
        " 공식 LMS 주소를 사용합니다.</p>"
    )


def render_moodle_connect_form(
    *,
    token: str,
    moodle_connect_path: str,
    school_name: str = "",
    username: str = "",
    error: str | None = None,
    school_options: list[dict[str, Any]] | None = None,
    resolved_school: dict[str, Any] | None = None,
    resolved_portal_info: dict[str, Any] | None = None,
    resolved_support: dict[str, Any] | None = None,
    resolved_is_uos_school: bool = False,
) -> bytes:
    error_html = (
        f'<div class="error">{html.escape(str(error or "").strip())}</div>' if error else ""
    )
    selected_school_name = str(school_name or "").strip()
    visible_option_names = {
        str(item.get("display_name") or "").strip()
        for item in list(school_options or [])
        if str(item.get("display_name") or "").strip()
    }
    if resolved_school and (not selected_school_name or selected_school_name not in visible_option_names):
        selected_school_name = str(resolved_school.get("display_name") or "").strip()
    options_html = ['<option value="">학교를 선택하세요</option>']
    for item in list(school_options or []):
        display_name = str(item.get("display_name") or "").strip()
        if not display_name:
            continue
        selected = " selected" if display_name == selected_school_name else ""
        options_html.append(
            f'<option value="{html.escape(display_name)}"{selected}>{html.escape(display_name)}</option>'
        )
    options_markup = "".join(options_html)
    resolved_note = _resolved_school_note_html(
        resolved_school=resolved_school,
        resolved_portal_info=resolved_portal_info,
        resolved_support=resolved_support,
        resolved_is_uos_school=resolved_is_uos_school,
    )
    username_hint = (
        "학교 로그인 화면에서 쓰는 계정 식별자를 그대로 입력하세요. "
        "학번으로 로그인하는 학교는 학번, 포털 ID나 통합계정으로 로그인하는 학교는 그 ID를 입력하면 됩니다."
    )
    intro_html = (
        "<p>학교 계정으로 로그인하면 온라인강의실을 연결하고 시간표는 학교 공식 API로 자동 동기화합니다.</p>"
        if resolved_is_uos_school
        else "<p>학교 계정으로 로그인하면 온라인강의실 연결을 확인합니다. 학교별로 포털/대학행정 자동 연동 범위는 다를 수 있습니다.</p>"
    )
    notes_html = (
        """
<ul class="note">
  <li>학교는 목록에서만 선택할 수 있습니다. 공식으로 등록된 로그인 경로만 사용합니다.</li>
  <li>계정 칸에는 학교 로그인 화면에서 쓰는 학번 또는 ID를 그대로 입력하세요.</li>
  <li>서울시립대 시간표는 포털 세션 없이 학교 공식 API를 사용합니다.</li>
  <li>로그인 후 Telegram으로 완료 메시지가 전송됩니다.</li>
</ul>
"""
        if resolved_is_uos_school
        else """
<ul class="note">
  <li>학교는 목록에서만 선택할 수 있습니다. 공식으로 등록된 로그인 경로만 사용합니다.</li>
  <li>계정 칸에는 학교 로그인 화면에서 쓰는 학번 또는 ID를 그대로 입력하세요.</li>
  <li>공식 포털 정보가 등록된 학교는 같은 학교 계정을 쓰는 포털 정보를 함께 안내합니다.</li>
  <li>로그인 후 Telegram으로 완료 메시지가 전송됩니다.</li>
</ul>
"""
    )
    body_html = f"""
<h1>학교 계정 연결</h1>
{intro_html}
<p>입력한 비밀번호는 이 사용자의 온라인강의실 재인증이 필요할 때만 보안 저장소에 저장합니다.</p>
{resolved_note}
<form method="post" action="{moodle_connect_path}">
  <input type="hidden" name="token" value="{html.escape(token)}" />
  <label for="school_name">대학교</label>
  <select id="school_name" name="school_name" required>{options_markup}</select>
  <label for="username">학교 로그인 계정 (학번 또는 ID)</label>
  <input id="username" name="username" type="text" autocomplete="username" placeholder="학교 로그인 화면에 쓰는 값 그대로 입력" value="{html.escape(username)}" required />
  <p class="field-hint">{html.escape(username_hint)}</p>
  <label for="password">비밀번호</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required />
  <button type="submit">학교 계정 연결하기</button>
</form>
{error_html}
{notes_html}
"""
    return render_html_page("학교 계정 연결", body_html)


def render_moodle_connect_success(*, display_name: str) -> bytes:
    body_html = f"""
<h1>연결 완료</h1>
<p class="success">{html.escape(display_name)}</p>
<p>이 창을 닫고 Telegram으로 돌아가세요. 연결 결과를 메시지로 다시 보냅니다.</p>
"""
    return render_html_page("연결 완료", body_html)


def render_moodle_connect_invalid(reason: str) -> bytes:
    body_html = f"""
<h1>링크를 사용할 수 없습니다</h1>
<p>{html.escape(reason)}</p>
<p>Telegram에서 <code>/connect</code>를 다시 실행해 새 링크를 받으세요.</p>
"""
    return render_html_page("링크 만료", body_html)


def render_portal_connect_form(
    *,
    token: str,
    portal_connect_path: str,
    username: str = "",
    error: str | None = None,
) -> bytes:
    error_html = (
        f'<div class="error">{html.escape(str(error or "").strip())}</div>' if error else ""
    )
    body_html = f"""
<h1>서울시립대 포털 연결</h1>
<p>서울시립대 포털 계정으로 로그인하면, 공식 포털/대학행정 세션을 저장해 강의시간표를 읽어옵니다.</p>
<p>비밀번호는 로그인에만 사용되고 저장하지 않습니다.</p>
<form method="post" action="{portal_connect_path}">
  <input type="hidden" name="token" value="{html.escape(token)}" />
  <label for="username">포털 로그인 ID</label>
  <input id="username" name="username" type="text" autocomplete="username" placeholder="학번 또는 포털 ID" value="{html.escape(username)}" required />
  <label for="password">비밀번호</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required />
  <button type="submit">포털 연결하기</button>
</form>
{error_html}
<ul class="note">
  <li>입력한 계정으로 포털 일반로그인을 시도한 뒤, 대학행정(WISE) 세션까지 연결합니다.</li>
  <li>연결이 완료되면 Telegram으로 결과 메시지를 보냅니다.</li>
  <li>연결 후 강의시간표는 공식 포털 데이터를 사용합니다.</li>
</ul>
"""
    return render_html_page("서울시립대 포털 연결", body_html)


def render_portal_connect_success(*, display_name: str) -> bytes:
    body_html = f"""
<h1>연결 완료</h1>
<p class="success">{html.escape(display_name)}</p>
<p>이 창을 닫고 Telegram으로 돌아가세요. 포털 세션 연결 결과를 메시지로 다시 보냅니다.</p>
"""
    return render_html_page("포털 연결 완료", body_html)


def render_portal_connect_invalid(reason: str) -> bytes:
    body_html = f"""
<h1>포털 연결 링크를 사용할 수 없습니다</h1>
<p>{html.escape(reason)}</p>
<p>Telegram에서 <code>/connect</code>를 다시 실행해 새 링크를 받으세요.</p>
"""
    return render_html_page("포털 연결 만료", body_html)


def render_browser_connect_page(
    *,
    token: str,
    display_name: str,
    browser_connect_path: str,
) -> bytes:
    frame_url = f"{browser_connect_path}/frame?{urlencode({'token': token, 'mode': 'img'})}"
    state_url = f"{browser_connect_path}/state?{urlencode({'token': token})}"
    action_url = f"{browser_connect_path}/action"
    complete_url = f"{browser_connect_path}/complete"
    body_html = f"""
<h1>브라우저 로그인 연결</h1>
<p><strong>{html.escape(display_name)}</strong> 로그인 화면을 원격 브라우저로 열었습니다.</p>
<p>화면을 클릭해 포커스를 잡은 뒤 키보드를 입력하세요. 로그인 후 <strong>연결 완료</strong>를 누르면 이 사용자 전용 세션을 저장합니다.</p>
<p>입력값은 저장하지 않고 이 원격 브라우저 세션에만 바로 전달합니다.</p>
<div id="status" class="field-hint">원격 브라우저 준비 중...</div>
<div style="margin-top: 1rem; border: 1px solid var(--line); border-radius: 1rem; overflow: hidden; background: #fff;">
  <div style="display:flex; align-items:center; justify-content:space-between; gap:0.6rem; padding:0.75rem 0.85rem; border-bottom:1px solid var(--line); background:rgba(20,83,45,0.04);">
    <strong style="font-size:0.96rem;">원격 화면</strong>
    <div style="display:flex; gap:0.45rem; flex-wrap:wrap;">
      <button id="zoom-out" type="button" style="width:auto; margin-top:0; padding:0.55rem 0.8rem;">축소</button>
      <button id="zoom-reset" type="button" style="width:auto; margin-top:0; padding:0.55rem 0.8rem;">맞춤</button>
      <button id="zoom-in" type="button" style="width:auto; margin-top:0; padding:0.55rem 0.8rem;">확대</button>
    </div>
  </div>
  <div id="remote-viewport" style="position:relative; overflow:auto; max-height:min(72vh, 52rem); background:#f8fafc; touch-action:pan-x pan-y;">
    <img id="remote-frame" src="{html.escape(frame_url)}" alt="remote browser" style="display:block; width:100%; max-width:none; height:auto; cursor:crosshair; touch-action:pan-x pan-y; user-select:none; -webkit-user-select:none;" />
    <div id="tap-indicator" style="display:none; position:absolute; width:1.35rem; height:1.35rem; margin-left:-0.675rem; margin-top:-0.675rem; border-radius:999px; border:2px solid rgba(217,119,6,0.95); background:rgba(217,119,6,0.16); box-shadow:0 0 0 9999px rgba(217,119,6,0.0); pointer-events:none;"></div>
  </div>
</div>
<div style="display:grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0.6rem; margin-top: 1rem;">
  <input id="type-input" type="text" inputmode="text" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="모바일에서는 여기에 ID/PW 일부를 입력한 뒤 전송" />
  <button id="type-send" type="button">입력</button>
</div>
<div style="display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.6rem; margin-top: 1rem;">
  <button type="button" data-press="Tab">Tab</button>
  <button type="button" data-press="Backspace">Backspace</button>
  <button type="button" data-press="Enter">Enter</button>
  <button type="button" data-press="ArrowUp">Up</button>
  <button type="button" data-press="ArrowDown">Down</button>
  <button type="button" data-action="reload">새로고침</button>
</div>
<button id="complete-button" type="button">연결 완료</button>
<ul class="note">
  <li>사용 순서: 화면 클릭 -> 키보드 입력 -> 학교 사이트 로그인 -> 연결 완료.</li>
  <li>현재 페이지는 실제 학교 로그인 화면을 원격 브라우저로 띄운 것입니다.</li>
  <li>로그인이 아직 안 됐으면 연결 완료 대신 계속 입력하거나 새로고침을 눌러 다시 확인하세요.</li>
</ul>
<script>
const token = {json.dumps(token)};
const frame = document.getElementById("remote-frame");
const viewport = document.getElementById("remote-viewport");
const statusBox = document.getElementById("status");
const tapIndicator = document.getElementById("tap-indicator");
const typeInput = document.getElementById("type-input");
const frameBaseUrl = {json.dumps(frame_url)};
const stateUrl = {json.dumps(state_url)};
const actionUrl = {json.dumps(action_url)};
const completeUrl = {json.dumps(complete_url)};
let refreshNonce = 0;
let keyboardCapture = false;
let actionInFlight = false;
let frameRefreshInFlight = false;
let remotePending = true;
let zoomPercent = window.innerWidth <= 768 ? 160 : 100;
let pointerStart = null;
let localTypingMode = false;

function applyZoom() {{
  frame.style.width = String(zoomPercent) + "%";
}}

function nextFrameUrl() {{
  refreshNonce += 1;
  return frameBaseUrl + "&ts=" + String(refreshNonce);
}}

async function fetchState() {{
  const response = await fetch(stateUrl, {{ cache: "no-store" }});
  if (!response.ok) {{
    statusBox.textContent = "원격 브라우저 상태를 읽지 못했습니다.";
    return;
  }}
  const payload = await response.json();
  remotePending = Boolean(payload.pending);
  if (payload.pending) {{
    statusBox.textContent = "원격 브라우저를 준비 중입니다...";
    return;
  }}
  const title = payload.title ? " | " + payload.title : "";
  statusBox.textContent = (payload.current_url || "") + title;
}}

async function refreshFrame(force = false) {{
  if (frameRefreshInFlight || remotePending || localTypingMode) {{
    return;
  }}
  frameRefreshInFlight = true;
  try {{
    frame.src = nextFrameUrl();
  }} finally {{
    window.setTimeout(() => {{
      frameRefreshInFlight = false;
    }}, force ? 150 : 350);
  }}
}}

function markTap(clientX, clientY) {{
  const rect = frame.getBoundingClientRect();
  const x = clientX - rect.left + viewport.scrollLeft;
  const y = clientY - rect.top + viewport.scrollTop;
  tapIndicator.style.display = "block";
  tapIndicator.style.left = String(x) + "px";
  tapIndicator.style.top = String(y) + "px";
  window.clearTimeout(markTap._timerId || 0);
  markTap._timerId = window.setTimeout(() => {{
    tapIndicator.style.display = "none";
  }}, 900);
}}

async function sendAction(payload) {{
  if (actionInFlight) {{
    return;
  }}
  actionInFlight = true;
  statusBox.textContent = "원격 브라우저에 입력 중...";
  try {{
    const response = await fetch(actionUrl, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ token, ...payload }}),
    }});
    if (!response.ok) {{
      statusBox.textContent = "원격 브라우저 제어에 실패했습니다.";
      return;
    }}
    await fetchState();
    if (!remotePending) {{
      await refreshFrame(true);
    }}
  }} finally {{
    actionInFlight = false;
  }}
}}

async function clickRemoteFrame(clientX, clientY) {{
  keyboardCapture = true;
  localTypingMode = false;
  if (document.activeElement === typeInput) {{
    typeInput.blur();
  }}
  const rect = frame.getBoundingClientRect();
  const naturalWidth = frame.naturalWidth || rect.width;
  const naturalHeight = frame.naturalHeight || rect.height;
  if (!rect.width || !rect.height) {{
    return;
  }}
  const x = ((clientX - rect.left) / rect.width) * naturalWidth;
  const y = ((clientY - rect.top) / rect.height) * naturalHeight;
  markTap(clientX, clientY);
  await sendAction({{ action: "click", x, y }});
}}

function isTapCandidate(startX, startY, endX, endY) {{
  return Math.abs(endX - startX) <= 12 && Math.abs(endY - startY) <= 12;
}}

frame.addEventListener("pointerdown", (event) => {{
  pointerStart = {{
    x: event.clientX,
    y: event.clientY,
  }};
}});

frame.addEventListener("pointerup", async (event) => {{
  const start = pointerStart;
  pointerStart = null;
  if (!start || !isTapCandidate(start.x, start.y, event.clientX, event.clientY)) {{
    return;
  }}
  event.preventDefault();
  await clickRemoteFrame(event.clientX, event.clientY);
}});

frame.addEventListener("touchstart", (event) => {{
  const touch = event.touches && event.touches[0];
  if (!touch) {{
    return;
  }}
  pointerStart = {{
    x: touch.clientX,
    y: touch.clientY,
  }};
}}, {{ passive: true }});

frame.addEventListener("touchend", async (event) => {{
  const touch = event.changedTouches && event.changedTouches[0];
  if (!touch) {{
    return;
  }}
  const start = pointerStart;
  pointerStart = null;
  if (!start || !isTapCandidate(start.x, start.y, touch.clientX, touch.clientY)) {{
    return;
  }}
  event.preventDefault();
  await clickRemoteFrame(touch.clientX, touch.clientY);
}}, {{ passive: false }});

document.getElementById("zoom-in").addEventListener("click", () => {{
  zoomPercent = Math.min(zoomPercent + 25, 300);
  applyZoom();
}});

document.getElementById("zoom-out").addEventListener("click", () => {{
  zoomPercent = Math.max(zoomPercent - 25, 75);
  applyZoom();
}});

document.getElementById("zoom-reset").addEventListener("click", () => {{
  zoomPercent = 100;
  applyZoom();
}});

for (const button of document.querySelectorAll("button[data-press]")) {{
  button.addEventListener("click", async () => {{
    keyboardCapture = true;
    await sendAction({{ action: "press", key: button.dataset.press }});
  }});
}}
for (const button of document.querySelectorAll("button[data-action]")) {{
  button.addEventListener("click", async () => {{
    await sendAction({{ action: button.dataset.action }});
  }});
}}

document.addEventListener("keydown", async (event) => {{
  if (!keyboardCapture) {{
    return;
  }}
  if (event.metaKey || event.ctrlKey || event.altKey) {{
    return;
  }}
  event.preventDefault();
  const key = event.key || "";
  if (key.length === 1) {{
    await sendAction({{ action: "type", text: key }});
    return;
  }}
  const supported = new Set(["Enter", "Backspace", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Escape"]);
  if (supported.has(key)) {{
    await sendAction({{ action: "press", key }});
  }}
}});

async function sendTypedInput() {{
  const value = typeInput.value || "";
  if (!value) {{
    return;
  }}
  keyboardCapture = false;
  await sendAction({{ action: "type", text: value }});
  typeInput.value = "";
  typeInput.focus();
}}

function enableLocalTypingMode() {{
  keyboardCapture = false;
  localTypingMode = true;
  statusBox.textContent = "여기에 입력한 뒤 `입력` 버튼을 누르면 원격 브라우저로 전달합니다.";
}}

function focusTypeInput() {{
  enableLocalTypingMode();
  window.requestAnimationFrame(() => {{
    typeInput.focus({{ preventScroll: true }});
  }});
}}

document.getElementById("type-send").addEventListener("click", async () => {{
  await sendTypedInput();
}});

typeInput.addEventListener("focus", () => {{
  enableLocalTypingMode();
}});

typeInput.addEventListener("blur", () => {{
  window.setTimeout(() => {{
    if (document.activeElement !== typeInput) {{
      localTypingMode = false;
      if (!remotePending) {{
        void refreshFrame(true);
      }}
    }}
  }}, 250);
}});

for (const eventName of ["click", "touchstart", "touchend", "pointerdown", "pointerup"]) {{
  typeInput.addEventListener(eventName, () => {{
    focusTypeInput();
  }}, {{ passive: true }});
}}

typeInput.addEventListener("keydown", async (event) => {{
  if (event.key !== "Enter") {{
    return;
  }}
  event.preventDefault();
  await sendTypedInput();
}});

document.getElementById("complete-button").addEventListener("click", async () => {{
  const response = await fetch(completeUrl, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ token }}),
  }});
  const text = await response.text();
  document.open();
  document.write(text);
  document.close();
}});

applyZoom();
fetchState();
setInterval(async () => {{
  if (localTypingMode) {{
    return;
  }}
  await fetchState();
  if (!remotePending) {{
    await refreshFrame(false);
  }}
}}, 1500);
</script>
"""
    return render_html_page("브라우저 로그인 연결", body_html)


def render_browser_connect_invalid(reason: str) -> bytes:
    body_html = f"""
<h1>브라우저 연결 링크를 사용할 수 없습니다</h1>
<p>{html.escape(reason)}</p>
<p>Telegram에서 <code>/connect</code>를 다시 실행해 새 링크를 받으세요.</p>
"""
    return render_html_page("브라우저 연결 만료", body_html)


def render_browser_frame_placeholder_svg(message: str) -> bytes:
    safe_message = html.escape(str(message or "").strip() or "원격 브라우저를 준비 중입니다...")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1400" viewBox="0 0 900 1400">
  <rect width="900" height="1400" fill="#f8fafc"/>
  <rect x="60" y="80" width="780" height="1240" rx="28" fill="#ffffff" stroke="#d4c7b3" stroke-width="2"/>
  <circle cx="130" cy="135" r="10" fill="#d97706"/>
  <circle cx="165" cy="135" r="10" fill="#14532d"/>
  <circle cx="200" cy="135" r="10" fill="#1d4ed8"/>
  <text x="450" y="700" text-anchor="middle" font-family="Apple SD Gothic Neo, Noto Sans KR, sans-serif" font-size="36" fill="#374151">{safe_message}</text>
  <text x="450" y="760" text-anchor="middle" font-family="Apple SD Gothic Neo, Noto Sans KR, sans-serif" font-size="24" fill="#6b7280">로그인 화면이 열리면 여기에 표시됩니다.</text>
</svg>"""
    return svg.encode("utf-8")
