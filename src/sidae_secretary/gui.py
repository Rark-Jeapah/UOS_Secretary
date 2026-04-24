from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Literal


JobName = Literal["sync_all", "sync_telegram", "status"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def select_gui_config_path(config_file: Path | None = None) -> Path:
    selected = config_file or Path(os.getenv("SIDAE_CONFIG_FILE", "config.toml"))
    if not selected.is_absolute():
        selected = _repo_root() / selected
    return selected.expanduser().resolve()


def build_cli_command(
    job_name: JobName,
    *,
    python_executable: str,
    config_file: Path | None,
) -> list[str]:
    command = [str(python_executable), "-m", "sidae_secretary.cli"]
    if job_name == "sync_all":
        command.extend(["sync", "--all", "--wait", "--timeout", "600"])
    elif job_name == "sync_telegram":
        command.extend(["sync-telegram", "--wait", "--timeout", "120"])
    elif job_name == "status":
        command.append("status")
    else:  # pragma: no cover
        raise ValueError(f"Unsupported job_name: {job_name}")
    if config_file is not None:
        command.extend(["--config-file", str(config_file)])
    return command


def _terminal_command(
    *,
    job_name: JobName,
    python_executable: str,
    config_file: Path,
) -> str:
    command = build_cli_command(
        job_name,
        python_executable=python_executable,
        config_file=config_file,
    )
    repo_root = _repo_root()
    src_path = repo_root / "src"
    log_file = f"/tmp/sidae_gui_{job_name}.log"
    helper_python = "/usr/bin/python3"
    command_text = " ".join(shlex.quote(part) for part in command)
    helper_text = " ".join(
        [
            shlex.quote(helper_python),
            "-m",
            "sidae_secretary.gui",
            "--show-summary",
            "--job",
            shlex.quote(job_name),
            "--log-file",
            shlex.quote(log_file),
            "--exit-code",
            '"${status}"',
        ]
    )
    prefix = " && ".join(
        [
            f"cd {shlex.quote(str(repo_root))}",
            f"export PYTHONPATH={shlex.quote(str(src_path))}${{PYTHONPATH:+:$PYTHONPATH}}",
            "set -o pipefail",
            f"rm -f {shlex.quote(log_file)}",
        ]
    )
    suffix = " ; ".join(
        [
            f"{command_text} 2>&1 | tee {shlex.quote(log_file)}",
            "status=$?",
            helper_text,
            f"rm -f {shlex.quote(log_file)}",
            "exit ${status}",
        ]
    )
    return prefix + " ; " + suffix


def _osascript_literal(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _launch_in_terminal(command_text: str, window_title: str) -> None:
    script_lines = [
        'tell application "Terminal"',
        "activate",
        f'do script "printf \'\\\\e]1;{_osascript_literal(window_title)}\\\\a\'; clear; {_osascript_literal(command_text)}"',
        "end tell",
    ]
    args: list[str] = ["osascript"]
    for line in script_lines:
        args.extend(["-e", line])
    subprocess.run(args, check=True)


def _extract_last_json_object(text: str) -> dict[str, object] | None:
    decoder = json.JSONDecoder()
    best_obj: dict[str, object] | None = None
    best_end = -1
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, consumed = decoder.raw_decode(text[index:])
        except Exception:
            continue
        absolute_end = index + consumed
        if isinstance(obj, dict) and absolute_end >= best_end:
            best_obj = obj
            best_end = absolute_end
    return best_obj


def _format_sync_all_summary(payload: dict[str, object], exit_code: int) -> tuple[str, str]:
    ok = bool(payload.get("ok")) and exit_code == 0
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    lines = ["전체 동기화가 완료되었습니다." if ok else "전체 동기화가 일부 실패했거나 중단되었습니다."]
    sync_portal = stats.get("sync_uos_portal_timetable") if isinstance(stats.get("sync_uos_portal_timetable"), dict) else {}
    sync_portal = sync_portal if isinstance(sync_portal, dict) else {}
    portal_events = int(sync_portal.get("upserted_events") or 0)
    if portal_events:
        lines.append(f"- 포털 시간표 {portal_events}건 반영")
    sync_uclass = stats.get("sync_uclass") if isinstance(stats.get("sync_uclass"), dict) else {}
    sync_uclass = sync_uclass if isinstance(sync_uclass, dict) else {}
    if sync_uclass:
        recorded = int(sync_uclass.get("recorded_artifacts") or 0)
        downloaded = int(sync_uclass.get("downloaded_artifacts") or 0)
        reused = int(sync_uclass.get("reused_artifacts") or 0)
        briefs = int(sync_uclass.get("generated_material_briefs") or 0)
        material_tasks = int(sync_uclass.get("detected_material_tasks") or 0)
        html_candidates = int(sync_uclass.get("html_material_candidates") or 0)
        if recorded or html_candidates:
            lines.append(
                f"- 유클래스 자료 {recorded or html_candidates}건 확인"
                f" (신규 다운로드 {downloaded}건, 재사용 {reused}건, 새 요약 {briefs}건, 파일 기반 과제 {material_tasks}건)"
            )
    dashboard = stats.get("publish_dashboard") if isinstance(stats.get("publish_dashboard"), dict) else {}
    dashboard = dashboard if isinstance(dashboard, dict) else {}
    if dashboard:
        lines.append("- 로컬 대시보드 갱신 완료")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        lines.append("")
        lines.append("오류:")
        for item in errors[:3]:
            lines.append(f"- {str(item)}")
    return ("시대비서: 전체 동기화", "\n".join(lines))


def _format_sync_telegram_summary(payload: dict[str, object], exit_code: int) -> tuple[str, str]:
    ok = bool(payload.get("ok")) and exit_code == 0
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    commands = stats.get("commands") if isinstance(stats.get("commands"), dict) else {}
    commands = commands if isinstance(commands, dict) else {}
    reminders = stats.get("reminders") if isinstance(stats.get("reminders"), dict) else {}
    reminders = reminders if isinstance(reminders, dict) else {}
    lines = ["텔레그램 처리 작업이 완료되었습니다." if ok else "텔레그램 처리 작업이 실패했습니다."]
    lines.append(f"- 새 업데이트 {int(stats.get('fetched_updates') or 0)}건 확인")
    lines.append(f"- 새 메시지 저장 {int(stats.get('stored_messages') or 0)}건")
    lines.append(f"- 명령 처리 {int(commands.get('processed') or 0)}건")
    lines.append(f"- 리마인더 발송 {int(reminders.get('sent') or 0)}건")
    return ("시대비서: 텔레그램 처리", "\n".join(lines))


def _format_status_summary(payload: dict[str, object], exit_code: int) -> tuple[str, str]:
    ok = exit_code == 0
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    lines = ["상태 조회가 완료되었습니다." if ok else "상태 조회가 실패했습니다."]
    lines.append(f"- 일정 {int(counts.get('events') or 0)}건")
    lines.append(f"- 과제 {int(counts.get('tasks') or 0)}건")
    lines.append(f"- inbox {int(counts.get('inbox') or 0)}건")
    return ("시대비서: 상태 확인", "\n".join(lines))


def _format_result_summary(job_name: str, payload: dict[str, object] | None, exit_code: int) -> tuple[str, str]:
    if payload is None:
        title = "시대비서: 작업 결과"
        body = (
            "작업은 끝났지만 결과 JSON을 읽지 못했습니다.\n"
            f"종료 코드: {exit_code}\n"
            "Terminal 로그를 확인하세요."
        )
        return title, body
    if job_name == "sync_all":
        return _format_sync_all_summary(payload, exit_code)
    if job_name == "sync_telegram":
        return _format_sync_telegram_summary(payload, exit_code)
    if job_name == "status":
        return _format_status_summary(payload, exit_code)
    return ("시대비서: 작업 결과", json.dumps(payload, ensure_ascii=False, indent=2))


def _display_dialog(title: str, message: str) -> None:
    script_lines = [
        f'display dialog "{_osascript_literal(message)}" with title "{_osascript_literal(title)}" buttons {{"확인"}} default button "확인"',
    ]
    args: list[str] = ["osascript"]
    for line in script_lines:
        args.extend(["-e", line])
    subprocess.run(args, check=False)


def show_summary_dialog(job_name: str, log_file: Path, exit_code: int) -> None:
    text = ""
    try:
        text = log_file.read_text(encoding="utf-8")
    except Exception:
        text = ""
    payload = _extract_last_json_object(text)
    title, body = _format_result_summary(job_name, payload, exit_code)
    _display_dialog(title, body)


def launch_gui(
    *,
    config_file: Path | None = None,
    python_executable: str | None = None,
) -> None:
    resolved_config = select_gui_config_path(config_file=config_file)
    app_python = str(python_executable or sys.executable)

    action_map: dict[str, tuple[JobName, str]] = {
        "전체 동기화": ("sync_all", "시대비서 - 전체 동기화"),
        "텔레그램만 처리": ("sync_telegram", "시대비서 - 텔레그램 처리"),
        "상태 확인": ("status", "시대비서 - 상태 확인"),
    }
    action_items = list(action_map.keys())
    items_literal = ", ".join(f'"{_osascript_literal(item)}"' for item in action_items)
    prompt = _osascript_literal(
        "실행할 작업을 선택하세요.\n\n선택한 작업은 새 Terminal 창에서 실행되고, 끝나면 한국어 요약 팝업이 뜹니다."
    )
    script_lines = [
        f"set actionItems to {{{items_literal}}}",
        f'set chosen to choose from list actionItems with title "시대비서" with prompt "{prompt}" without multiple selections allowed',
        "if chosen is false then",
        "return",
        "end if",
        "set selectedAction to item 1 of chosen",
        "return selectedAction",
    ]
    args: list[str] = ["osascript"]
    for line in script_lines:
        args.extend(["-e", line])
    completed = subprocess.run(args, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "AppleScript chooser failed")
    selected_action = str(completed.stdout or "").strip()
    if not selected_action:
        return
    if selected_action not in action_map:
        raise RuntimeError(f"Unknown GUI action: {selected_action}")
    job_name, window_title = action_map[selected_action]
    command_text = _terminal_command(
        job_name=job_name,
        python_executable=app_python,
        config_file=resolved_config,
    )
    _launch_in_terminal(command_text, window_title)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="시대비서 수동 제어 패널")
    parser.add_argument("--config-file", default="", help="설정 파일 경로")
    parser.add_argument("--python-bin", default="", help="실제 작업 실행용 Python 경로")
    parser.add_argument("--show-summary", action="store_true", help="작업 완료 후 요약 팝업 표시")
    parser.add_argument("--job", default="", help="요약 대상 작업 이름")
    parser.add_argument("--log-file", default="", help="요약 대상 로그 파일 경로")
    parser.add_argument("--exit-code", default="0", help="원래 작업 종료 코드")
    args = parser.parse_args(argv)

    if bool(args.show_summary):
        if not str(args.job).strip() or not str(args.log_file).strip():
            raise SystemExit("--show-summary requires --job and --log-file")
        show_summary_dialog(
            str(args.job).strip(),  # type: ignore[arg-type]
            Path(str(args.log_file)).expanduser(),
            int(str(args.exit_code or "0")),
        )
        return

    config_file = Path(args.config_file).expanduser() if str(args.config_file).strip() else None
    python_bin = str(args.python_bin).strip() or None
    launch_gui(config_file=config_file, python_executable=python_bin)


if __name__ == "__main__":
    main()
