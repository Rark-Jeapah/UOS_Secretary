from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from sidae_secretary import cli

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def _hold_sync_lock(lock_path: str, ready: mp.Event, release: mp.Event) -> None:
    if fcntl is None:
        return
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        ready.set()
        release.wait(timeout=30)
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def _hold_sync_lock_for_duration(lock_path: str, ready: mp.Event, hold_seconds: float) -> None:
    if fcntl is None:
        return
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        ready.set()
        time.sleep(max(float(hold_seconds), 0.01))
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


@pytest.mark.skipif(os.name == "nt" or fcntl is None, reason="requires POSIX fcntl")
def test_sync_all_exits_fast_when_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    lock_path = cli._sync_all_lock_path(db_path)

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(target=_hold_sync_lock, args=(str(lock_path), ready, release))
    proc.start()
    assert ready.wait(timeout=10)

    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    def _run_all_jobs_unexpected(settings, db):
        raise AssertionError("run_all_jobs should not run while sync lock is held")

    monkeypatch.setattr(cli, "run_all_jobs", _run_all_jobs_unexpected)

    try:
        started = time.monotonic()
        result = runner.invoke(cli.app, ["sync", "--all"])
        elapsed = time.monotonic() - started
    finally:
        release.set()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)

    assert proc.exitcode == 0
    assert result.exit_code == 4
    assert elapsed < 1.0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "sync_lock_held"
    assert payload["lock_path"] == str(lock_path)


@pytest.mark.skipif(os.name == "nt" or fcntl is None, reason="requires POSIX fcntl")
def test_sync_all_wait_blocks_then_runs_once(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    lock_path = cli._sync_all_lock_path(db_path)

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_hold_sync_lock_for_duration,
        args=(str(lock_path), ready, 0.2),
    )
    proc.start()
    assert ready.wait(timeout=10)

    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    run_calls: list[float] = []

    def _run_all_jobs_once(settings, db):
        run_calls.append(time.monotonic())
        return SimpleNamespace(ok=True, stats={"sync_uclass": {"fetched_notifications": 1}}, errors=[])

    monkeypatch.setattr(cli, "run_all_jobs", _run_all_jobs_once)

    result_box: dict[str, object] = {}

    def _invoke() -> None:
        started = time.monotonic()
        result_box["result"] = runner.invoke(
            cli.app,
            ["sync", "--all", "--wait", "--timeout-seconds", "2"],
        )
        result_box["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=_invoke, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        pytest.fail("sync --all --wait did not complete within timeout")

    proc.join(timeout=10)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
        pytest.fail("lock holder process did not terminate")
    assert proc.exitcode == 0

    result = result_box["result"]
    assert result is not None
    assert result.exit_code == 0
    assert result_box["elapsed"] >= 0.15
    assert len(run_calls) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


@pytest.mark.skipif(os.name == "nt" or fcntl is None, reason="requires POSIX fcntl")
def test_sync_all_wait_timeout_returns_lock_timeout(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    lock_path = cli._sync_all_lock_path(db_path)

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_hold_sync_lock_for_duration,
        args=(str(lock_path), ready, 0.4),
    )
    proc.start()
    assert ready.wait(timeout=10)

    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    def _run_all_jobs_unexpected(settings, db):
        raise AssertionError("run_all_jobs should not run before lock timeout")

    monkeypatch.setattr(cli, "run_all_jobs", _run_all_jobs_unexpected)

    started = time.monotonic()
    result = runner.invoke(
        cli.app,
        ["sync", "--all", "--wait", "--timeout-seconds", "0.1"],
    )
    elapsed = time.monotonic() - started

    proc.join(timeout=10)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
        pytest.fail("lock holder process did not terminate")
    assert proc.exitcode == 0

    assert result.exit_code == 4
    assert elapsed >= 0.09
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "sync_lock_timeout"
    assert payload["lock_path"] == str(lock_path)
    assert float(payload["waited_seconds"]) >= 0.1


@pytest.mark.skipif(os.name == "nt" or fcntl is None, reason="requires POSIX fcntl")
def test_sync_all_skips_telegram_job_when_listener_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    lock_path = cli._sync_telegram_lock_path(db_path)

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(target=_hold_sync_lock, args=(str(lock_path), ready, release))
    proc.start()
    assert ready.wait(timeout=10)

    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    job_calls: list[str] = []

    def _run_all_jobs_once(settings, db, *, job_runner=None):
        assert job_runner is not None

        def _sync_portal(settings, db):
            job_calls.append("sync_uos_portal_timetable")
            return {"ok": True}

        def _sync_telegram_unexpected(settings, db):
            job_calls.append("sync_telegram")
            raise AssertionError("sync_telegram should be skipped while listener lock is held")

        stats = {
            "sync_uos_portal_timetable": job_runner("sync_uos_portal_timetable", _sync_portal),
            "sync_telegram": job_runner("sync_telegram", _sync_telegram_unexpected),
        }
        return SimpleNamespace(ok=True, stats=stats, errors=[])

    monkeypatch.setattr(cli, "run_all_jobs", _run_all_jobs_once)

    try:
        started = time.monotonic()
        result = runner.invoke(
            cli.app,
            ["sync", "--all", "--wait", "--timeout-seconds", "2"],
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)

    assert proc.exitcode == 0
    assert result.exit_code == 0
    assert elapsed < 1.0
    assert job_calls == ["sync_uos_portal_timetable"]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["stats"]["sync_telegram"]["skipped"] is True
    assert payload["stats"]["sync_telegram"]["reason"] == "sync_telegram_lock_held"
    assert payload["stats"]["sync_telegram"]["lock_path"] == str(lock_path)


def test_sync_telegram_command_runs_once(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    calls: list[float] = []

    def _sync_telegram_once(settings, db):
        calls.append(time.monotonic())
        return {"fetched_updates": 1, "stored_messages": 1}

    monkeypatch.setattr(cli, "sync_telegram_job", _sync_telegram_once)

    result = runner.invoke(cli.app, ["sync-telegram"])

    assert result.exit_code == 0
    assert len(calls) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["stats"]["fetched_updates"] == 1
    assert payload["stats"]["stored_messages"] == 1


def test_sync_uclass_command_runs_once(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    uclass_calls: list[float] = []

    def _sync_uclass_once(settings, db):
        uclass_calls.append(time.monotonic())
        return {"upserted_notifications": 2}

    monkeypatch.setattr(cli, "sync_uclass_job", _sync_uclass_once)

    result = runner.invoke(cli.app, ["sync-uclass"])

    assert result.exit_code == 0
    assert len(uclass_calls) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["stats"]["sync_uclass"]["upserted_notifications"] == 2


def test_sync_weather_command_runs_once(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    weather_calls: list[float] = []

    def _sync_weather_once(settings, db):
        weather_calls.append(time.monotonic())
        return {"ok": True, "condition_text": "맑음"}

    monkeypatch.setattr(cli, "sync_weather_job", _sync_weather_once)

    result = runner.invoke(cli.app, ["sync-weather"])

    assert result.exit_code == 0
    assert len(weather_calls) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["stats"]["condition_text"] == "맑음"


def test_send_briefings_command_runs_once(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)
    calls: list[float] = []

    def _send_briefings_once(settings, db):
        calls.append(time.monotonic())
        return {"sent_slots": ["morning"], "results": {"morning": {"sent_to": ["12345"]}}}

    monkeypatch.setattr(cli, "send_scheduled_briefings_job", _send_briefings_once)

    result = runner.invoke(cli.app, ["send-briefings"])

    assert result.exit_code == 0
    assert len(calls) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["stats"]["sent_slots"] == ["morning"]


def test_telegram_listener_once_runs_one_cycle(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    settings = SimpleNamespace(
        database_path=db_path,
        telegram_enabled=True,
        telegram_bot_token="token",
        briefing_enabled=True,
        briefing_channel="telegram",
        briefing_delivery_mode="direct",
    )
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    cycle_calls: list[float] = []

    def _listener_sync(settings, db, *, client, poll_timeout_seconds):
        cycle_calls.append(time.monotonic())
        return {
            "fetched_updates": 1,
            "stored_messages": 1,
            "commands": {"processed": 1, "failed": 0},
            "reminders": {"due": 0, "sent": 0, "failed": 0},
        }

    def _send_briefings(settings, db, *, wait=False, timeout_seconds=None):
        return {"ok": True, "stats": {"sent_slots": ["morning"]}, "errors": []}

    monkeypatch.setattr(cli, "_telegram_listener_sync", _listener_sync)
    monkeypatch.setattr(cli, "_run_send_briefings_once", _send_briefings)

    result = runner.invoke(cli.app, ["telegram-listener", "--once"])

    assert result.exit_code == 0
    assert len(cycle_calls) == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["cycles"] == 1
    assert payload["report"]["telegram"]["fetched_updates"] == 1
    assert payload["report"]["briefings"]["stats"]["sent_slots"] == ["morning"]


@pytest.mark.skipif(os.name == "nt" or fcntl is None, reason="requires POSIX fcntl")
def test_sync_telegram_exits_fast_when_lock_is_held(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    lock_path = cli._sync_telegram_lock_path(db_path)

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(target=_hold_sync_lock, args=(str(lock_path), ready, release))
    proc.start()
    assert ready.wait(timeout=10)

    settings = SimpleNamespace(database_path=db_path)
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    def _sync_telegram_unexpected(settings, db):
        raise AssertionError("sync_telegram_job should not run while telegram lock is held")

    monkeypatch.setattr(cli, "sync_telegram_job", _sync_telegram_unexpected)

    try:
        started = time.monotonic()
        result = runner.invoke(cli.app, ["sync-telegram"])
        elapsed = time.monotonic() - started
    finally:
        release.set()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)

    assert proc.exitcode == 0
    assert result.exit_code == 4
    assert elapsed < 1.0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "sync_telegram_lock_held"
    assert payload["lock_path"] == str(lock_path)


def test_telegram_listener_exits_after_consecutive_errors(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    settings = SimpleNamespace(
        database_path=db_path,
        telegram_enabled=True,
        telegram_bot_token="token",
        briefing_enabled=False,
    )
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    cycle_calls: list[float] = []

    def _listener_sync(settings, db, *, client, poll_timeout_seconds):
        cycle_calls.append(time.monotonic())
        return {"error": "telegram unavailable"}

    monkeypatch.setattr(cli, "_telegram_listener_sync", _listener_sync)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    result = runner.invoke(
        cli.app,
        [
            "telegram-listener",
            "--poll-timeout-seconds",
            "10",
            "--error-backoff-seconds",
            "2",
            "--max-consecutive-errors",
            "3",
        ],
    )

    assert result.exit_code == 1
    assert len(cycle_calls) == 3


def test_telegram_listener_uses_poll_timeout_for_client_timeout(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    db_path = tmp_path / "sidae.db"
    settings = SimpleNamespace(
        database_path=db_path,
        telegram_enabled=True,
        telegram_bot_token="token",
        briefing_enabled=False,
    )
    monkeypatch.setattr(cli, "load_settings", lambda config_file=None: settings)

    created_clients: list[tuple[str, int]] = []

    class FakeTelegramClient:
        def __init__(self, bot_token: str, timeout_sec: int = 30):
            created_clients.append((bot_token, timeout_sec))

    monkeypatch.setattr(cli, "TelegramBotClient", FakeTelegramClient)
    monkeypatch.setattr(
        cli,
        "_telegram_listener_sync",
        lambda settings, db, *, client, poll_timeout_seconds: {
            "fetched_updates": 0,
            "stored_messages": 0,
            "commands": {"processed": 0, "failed": 0},
            "reminders": {"due": 0, "sent": 0, "failed": 0},
        },
    )

    result = runner.invoke(
        cli.app,
        ["telegram-listener", "--once", "--poll-timeout-seconds", "10"],
    )

    assert result.exit_code == 0
    assert created_clients == [("token", 10)]
