from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary import gui as gui_module


def test_upsert_env_value_updates_existing_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FOO=1\nLLM_LOCAL_ENDPOINT=http://127.0.0.1:11434/api/generate\nBAR=2\n",
        encoding="utf-8",
    )

    cli._upsert_env_value(
        env_file,
        "LLM_LOCAL_ENDPOINT",
        "http://127.0.0.1:11434/api/chat",
    )

    content = env_file.read_text(encoding="utf-8")
    assert "FOO=1" in content
    assert "BAR=2" in content
    assert "LLM_LOCAL_ENDPOINT=http://127.0.0.1:11434/api/chat" in content
    assert "LLM_LOCAL_ENDPOINT=http://127.0.0.1:11434/api/generate\n" not in content


def test_build_cli_command_for_gui_sync_all(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    command = gui_module.build_cli_command(
        "sync_all",
        python_executable="/opt/homebrew/bin/python3",
        config_file=config_file,
    )

    assert command == [
        "/opt/homebrew/bin/python3",
        "-m",
        "sidae_secretary.cli",
        "sync",
        "--all",
        "--wait",
        "--timeout",
        "600",
        "--config-file",
        str(config_file),
    ]


def test_terminal_command_runs_summary_helper(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"

    command_text = gui_module._terminal_command(
        job_name="sync_all",
        python_executable="/opt/homebrew/bin/python3",
        config_file=config_file,
    )

    assert "set -o pipefail" in command_text
    assert "--show-summary" in command_text
    assert "--job sync_all" in command_text
    assert "--exit-code \"${status}\"" in command_text


def test_extract_last_json_object_prefers_final_top_level_payload() -> None:
    text = "\n".join(
        [
            '{"ts":"2026-03-08T00:00:00Z","msg":"log"}',
            '{"ts":"2026-03-08T00:00:01Z","msg":"another"}',
            '{"ok": true, "stats": {"sync_uos_portal_timetable": {"upserted_events": 6}}, "errors": []}',
        ]
    )

    payload = gui_module._extract_last_json_object(text)

    assert payload == {
        "ok": True,
        "stats": {"sync_uos_portal_timetable": {"upserted_events": 6}},
        "errors": [],
    }


def test_format_sync_all_summary_in_korean() -> None:
    title, body = gui_module._format_result_summary(
        "sync_all",
        {
            "ok": True,
            "stats": {
                "sync_uos_portal_timetable": {"upserted_events": 6},
                "sync_uclass": {
                    "recorded_artifacts": 8,
                    "downloaded_artifacts": 0,
                    "reused_artifacts": 8,
                    "generated_material_briefs": 0,
                    "detected_material_tasks": 1,
                    "html_material_candidates": 8,
                },
                "publish_dashboard": {"html_path": "/tmp/index.html"},
            },
            "errors": [],
        },
        0,
    )

    assert title == "시대비서: 전체 동기화"
    assert "전체 동기화가 완료되었습니다." in body
    assert "포털 시간표 6건 반영" in body
    assert "유클래스 자료 8건 확인" in body
    assert "로컬 대시보드 갱신 완료" in body


def test_cli_still_exposes_top_level_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "sync" in result.stdout
    assert "relay" in result.stdout
