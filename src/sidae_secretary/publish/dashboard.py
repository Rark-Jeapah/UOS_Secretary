from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sidae_secretary.db import Database, now_utc_iso
from sidae_secretary.storage import dashboard_dir as storage_dashboard_dir
from sidae_secretary.storage import materials_dir as storage_materials_dir


HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>시대비서 스냅샷</title>
  <style>
    :root {
      --bg: #f7f8fb;
      --card: #ffffff;
      --text: #14151a;
      --muted: #5a6272;
      --accent: #1762d5;
      --accent-soft: #e9f1ff;
      --border: #e6e9f0;
      --warn-bg: #fff4e8;
      --warn-text: #9a4a00;
      --ok-bg: #eaf7ef;
      --ok-text: #17603a;
      --skip-bg: #eef2f7;
      --skip-text: #435063;
    }
    body { margin: 0; font-family: "Apple SD Gothic Neo","Noto Sans KR",sans-serif; background: linear-gradient(180deg,#f8fbff,#f5f7fb); color: var(--text); }
    .wrap { max-width: 980px; margin: 0 auto; padding: 28px 18px 42px; }
    h1 { margin: 0 0 6px; font-size: 28px; letter-spacing: 0.2px; }
    .meta { color: var(--muted); margin-bottom: 16px; }
    .grid { display: grid; gap: 14px; grid-template-columns: 1fr; }
    @media (min-width: 960px) { .grid { grid-template-columns: 1fr 1fr; } }
    section { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; box-shadow: 0 4px 10px rgba(8, 22, 54, 0.04); }
    h2 { margin: 0 0 10px; color: var(--accent); font-size: 18px; }
    ul { margin: 0; padding-left: 18px; }
    li { margin: 8px 0; }
    .small { color: var(--muted); font-size: 13px; }
    .tiny { color: var(--muted); font-size: 12px; }
    code { background: #f1f4f8; padding: 2px 6px; border-radius: 6px; font-size: 12px; }
    .sync-section { margin-bottom: 14px; background: linear-gradient(180deg,#ffffff,#f7faff); }
    .sync-summary { display: grid; gap: 8px; margin-bottom: 12px; }
    .sync-cards { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); }
    .sync-card { border: 1px solid var(--border); border-radius: 10px; padding: 10px; background: rgba(255,255,255,0.85); }
    .sync-card h3 { margin: 0 0 6px; font-size: 15px; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.2px; vertical-align: baseline; }
    .tag-ok { background: var(--ok-bg); color: var(--ok-text); }
    .tag-warn { background: var(--warn-bg); color: var(--warn-text); }
    .tag-skip { background: var(--skip-bg); color: var(--skip-text); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>시대비서</h1>
    <div class="meta" id="syncMeta">스냅샷을 불러오는 중...</div>
    <section class="sync-section">
      <h2>전체 상태</h2>
      <div id="syncOverview" class="small">동기화 상태를 불러오는 중...</div>
    </section>
    <div class="grid">
      <section>
        <h2>다가오는 일정</h2>
        <ul id="events"></ul>
      </section>
      <section>
        <h2>다가오는 과제</h2>
        <ul id="tasks"></ul>
      </section>
      <section>
        <h2>오늘 완료</h2>
        <ul id="doneToday"></ul>
      </section>
      <section>
        <h2>새 알림</h2>
        <ul id="notifications"></ul>
      </section>
      <section>
        <h2>날씨</h2>
        <ul id="weather"></ul>
      </section>
      <section>
        <h2>확인 필요</h2>
        <ul id="conflicts"></ul>
      </section>
      <section>
        <h2>최근 자료</h2>
        <ul id="materials"></ul>
      </section>
      <section>
        <h2>미처리 Inbox</h2>
        <ul id="inbox"></ul>
      </section>
      <section>
        <h2>요약</h2>
        <ul id="summaries"></ul>
      </section>
    </div>
  </div>
  <script id="sidae-data" type="application/json">__SIDAE_DATA_JSON__</script>
  <script>
    function fmt(value) {
      if (!value) return "-";
      try { return new Date(value).toLocaleString(); } catch { return value; }
    }
    function fillList(id, rows, render) {
      const el = document.getElementById(id);
      el.innerHTML = "";
      if (!rows || rows.length === 0) {
        const li = document.createElement("li");
        li.className = "small";
        li.textContent = "표시할 항목 없음";
        el.appendChild(li);
        return;
      }
      rows.forEach((row) => {
        const li = document.createElement("li");
        li.innerHTML = render(row);
        el.appendChild(li);
      });
    }
    function parseMeta(value) {
      if (!value) return {};
      if (typeof value === "object") return value;
      try { return JSON.parse(value); } catch { return {}; }
    }
    function esc(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }
    function provenanceHtml(row, options = {}) {
      const parsed = parseMeta(row && row.metadata_json);
      const provenance = row && row.provenance ? row.provenance : parsed.provenance;
      if (!provenance || !provenance.source) return "";
      const estimated = Boolean(provenance.is_estimate);
      const showBadge = options.showBadge !== false;
      const badge = showBadge
        ? "<span class='tag " + (estimated ? "tag-warn" : "tag-ok") + "'>" + esc(estimated ? "추정" : "공식") + "</span> "
        : "";
      const sourceLabel = provenance.source_label || provenance.source;
      const verified = provenance.last_verified_at ? " | 확인: " + esc(fmt(provenance.last_verified_at)) : "";
      const notice = estimated && provenance.notice
        ? "<br><span class='tiny'>" + esc(provenance.notice) + "</span>"
        : "";
      return "<br><span class='small'>" + badge + esc(sourceLabel) + verified + "</span>" + notice;
    }
    function syncStatusTag(status) {
      const normalized = String(status || "never").toLowerCase();
      if (normalized === "success") return "<span class='tag tag-ok'>정상</span>";
      if (normalized === "error") return "<span class='tag tag-warn'>문제 있음</span>";
      if (normalized === "skipped") return "<span class='tag tag-skip'>확인 필요</span>";
      return "<span class='tag tag-skip'>준비 전</span>";
    }
    function renderSyncOverview(syncDashboard) {
      const el = document.getElementById("syncOverview");
      if (!syncDashboard || !Array.isArray(syncDashboard.sources)) {
        el.textContent = "동기화 정보 없음";
        return;
      }
      const lastError = syncDashboard.last_error || null;
      const header = [
        "<div class='sync-summary'>",
        "<div>마지막 성공 동기화: <code>" + esc(syncDashboard.last_successful_sync_at || "기록 없음") + "</code></div>",
        "<div>확인 필요: <code>" + esc(syncDashboard.action_required_count || 0) + "</code> | inbox <code>" + esc(syncDashboard.pending_inbox_count || 0) + "</code> | 충돌 <code>" + esc(syncDashboard.conflict_warning_count || 0) + "</code></div>",
        lastError ? "<div>일부 동기화에 확인이 필요합니다.</div>" : "",
        "</div>",
      ].join("");
      const cards = syncDashboard.sources.map((item) => {
        const errorLine = item.last_error ? "<div class='tiny'>최근 확인이 필요합니다.</div>" : "";
        const attentionLine = item.action_required ? "<div class='tiny'>확인 필요 " + esc(item.action_required) + "건</div>" : "";
        return "<div class='sync-card'>" +
          "<h3>" + esc(item.label || item.key || "Source") + "</h3>" +
          "<div>" + syncStatusTag(item.status) + "</div>" +
          "<div class='small'>마지막 실행: " + esc(fmt(item.last_run_at)) + "</div>" +
          "<div class='small'>신규/갱신: " + esc(item.new_items || 0) + "</div>" +
          attentionLine +
          errorLine +
          "</div>";
      }).join("");
      el.innerHTML = header + "<div class='sync-cards'>" + cards + "</div>";
    }
    try {
      const raw = document.getElementById("sidae-data").textContent;
      const data = JSON.parse(raw || "{}");
        const syncDashboard = data.sync_dashboard || {};
        document.getElementById("syncMeta").innerHTML =
          "마지막 동기화: <code>" + esc(data.last_sync_at || "기록 없음") + "</code>" +
          " | 확인 필요: <code>" + esc(syncDashboard.action_required_count || 0) + "</code>";
        renderSyncOverview(syncDashboard);

        fillList("events", data.upcoming_events || [], (x) =>
          "<strong>" + (x.title || "일정") + "</strong><br><span class='small'>" +
          fmt(x.start_at) + " → " + fmt(x.end_at) + "</span>" +
          provenanceHtml(x)
        );
        fillList("tasks", data.due_tasks || [], (x) =>
          "<strong>" + (x.title || "과제") + "</strong><br><span class='small'>마감: " +
          fmt(x.due_at) + " | 상태: " + (x.status || "unknown") + "</span>" +
          provenanceHtml(x)
        );
        fillList("doneToday", data.done_today || [], (x) =>
          "<strong>" + (x.title || "완료 항목") + "</strong><br><span class='small'>상태: " +
          (x.status || "-") + " | 업데이트: " + fmt(x.updated_at) + "</span>" +
          provenanceHtml(x)
        );
        fillList("notifications", data.new_notifications || [], (x) =>
          "<strong>" + (x.title || "알림") + "</strong><br><span class='small'>" +
          fmt(x.created_at) + (x.url ? " | <a href='" + x.url + "' target='_blank'>열기</a>" : "") +
          "</span>"
        );
        fillList("weather", data.weather_snapshot ? [data.weather_snapshot] : [], (x) =>
          (() => {
            const current = x.current || {};
            const today = x.today || {};
            const morning = today.morning || {};
            const afternoon = today.afternoon || {};
            const air = x.air_quality || {};
            const districts = Array.isArray(air.districts) ? air.districts.slice(0, 2) : [];
            const parts = [];
            if (current.temperature_c != null) {
              parts.push("현재 " + esc(current.temperature_c) + "C " + esc(current.condition_text || ""));
            }
            if (today.temperature_min_c != null || today.temperature_max_c != null) {
              parts.push("오늘 " + esc(today.temperature_min_c ?? "?") + "C / " + esc(today.temperature_max_c ?? "?") + "C");
            }
            if (today.diurnal_range_c != null) {
              parts.push("일교차 " + esc(today.diurnal_range_c) + "C" + (today.diurnal_range_alert ? " (큼)" : ""));
            }
            const morningText = morning.label
              ? "<br><span class='small'>" + esc(morning.label) + ": " + esc(morning.temperature_min_c ?? "?") + "~" + esc(morning.temperature_max_c ?? "?") + "C, " + esc(morning.condition_text || "-") + ", 강수확률 " + esc(morning.precip_probability_max ?? "?") + "%</span>"
              : "";
            const afternoonText = afternoon.label
              ? "<br><span class='small'>" + esc(afternoon.label) + ": " + esc(afternoon.temperature_min_c ?? "?") + "~" + esc(afternoon.temperature_max_c ?? "?") + "C, " + esc(afternoon.condition_text || "-") + ", 강수확률 " + esc(afternoon.precip_probability_max ?? "?") + "%</span>"
              : "";
            const airText = districts.length
              ? districts.map((row) => "<br><span class='small'>미세먼지: " + esc(row.district_name || row.district_code || "-") + " " + esc(row.cai_grade || "-") + " | PM10 " + esc(row.pm10 ?? "?") + " | PM2.5 " + esc(row.pm25 ?? "?") + "</span>").join("")
              : "";
            const updated = x.generated_at ? "<br><span class='tiny'>갱신: " + esc(fmt(x.generated_at)) + "</span>" : "";
            return "<strong>" + esc(x.location_label || "날씨") + "</strong><br><span class='small'>" +
              esc(parts.join(" | ")) + "</span>" + morningText + afternoonText + airText + updated;
          })()
        );
        fillList("conflicts", data.conflict_warnings || [], (x) =>
          "<strong>" + (x.title || "확인 필요") + "</strong><br><span class='small'>" +
          fmt(x.created_at) + (x.body ? " | " + x.body : "") + "</span>"
        );
        fillList("materials", data.recent_materials || [], (x) =>
          (() => {
            const meta = parseMeta(x.metadata_json);
            const brief = meta.brief || {};
            const bullets = Array.isArray(brief.bullets) ? brief.bullets.slice(0, 2).join(" | ") : "";
            const question = brief.question ? "<br><span class='small'>Q: " + brief.question + "</span>" : "";
            return "<strong>" + (x.filename || "file") + "</strong><br><span class='small'>" +
              (x.icloud_path || "-") + "</span>" +
              (bullets ? "<br><span class='small'>" + bullets + "</span>" : "") +
              (question ? question.replace("Q: ", "복습 질문: ") : "") +
              provenanceHtml(x) +
              (x.brief_provenance ? provenanceHtml({ provenance: x.brief_provenance }, { showBadge: true }) : "");
          })()
        );
        fillList("inbox", data.inbox_unprocessed || [], (x) =>
          "<strong>" + (x.title || "Inbox 항목") + "</strong><br><span class='small'>" +
          fmt(x.received_at) + " | 유형: " + (x.item_type || "note") + "</span>"
        );
        fillList("summaries", data.summaries || [], (x) =>
          "<strong>" + (x.title || "요약") + "</strong><br><span class='small'>" +
          fmt(x.created_at) + "</span><br>" + (x.body || "") +
          (x.action_item ? "<br><span class='small'>다음 할 일: " + x.action_item + "</span>" : "") +
          provenanceHtml(x)
        );
    } catch (err) {
      document.getElementById("syncMeta").textContent = "스냅샷을 불러오지 못했습니다: " + err;
    }
  </script>
</body>
</html>
"""


def _write_precomputed_telegram_briefings(
    dashboard_dir: Path,
    payload: Any,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, dict):
        return None

    briefings_dir = dashboard_dir / "telegram_briefings"
    briefings_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = briefings_dir / "index.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    item_paths: dict[str, dict[str, str]] = {}
    for slot, item in items.items():
        if not isinstance(item, dict):
            continue
        slot_name = str(slot).strip().lower()
        if not slot_name:
            continue
        json_path = briefings_dir / f"{slot_name}.json"
        text_path = briefings_dir / f"{slot_name}.txt"
        json_path.write_text(
            json.dumps(item, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        message = str(item.get("message") or "").strip()
        if message:
            text_path.write_text(message.rstrip() + "\n", encoding="utf-8")
        else:
            text_path.write_text("", encoding="utf-8")
        item_paths[slot_name] = {
            "json_path": str(json_path),
            "text_path": str(text_path),
        }

    return {
        "manifest_path": str(manifest_path),
        "items": item_paths,
    }


def render_dashboard_snapshot(
    db: Database,
    storage_root_dir: Path,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dashboard_dir = storage_dashboard_dir(storage_root_dir)
    materials_dir = storage_materials_dir(storage_root_dir)
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    materials_dir.mkdir(parents=True, exist_ok=True)

    data = db.dashboard_snapshot()
    if extra_data:
        data.update(extra_data)
    if not data.get("last_sync_at"):
        data["last_sync_at"] = now_utc_iso()

    data_path = dashboard_dir / "data.json"
    html_path = dashboard_dir / "index.html"
    data_path.write_text(
        json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8"
    )
    embedded = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    html_content = HTML_TEMPLATE.replace("__SIDAE_DATA_JSON__", embedded)
    html_path.write_text(html_content, encoding="utf-8")
    result = {
        "dashboard_dir": str(dashboard_dir),
        "data_path": str(data_path),
        "html_path": str(html_path),
        "materials_dir": str(materials_dir),
    }
    precomputed_paths = _write_precomputed_telegram_briefings(
        dashboard_dir=dashboard_dir,
        payload=data.get("precomputed_telegram_briefings"),
    )
    if precomputed_paths is not None:
        result["telegram_briefing_files"] = precomputed_paths
    return result
