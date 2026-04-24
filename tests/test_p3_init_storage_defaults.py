from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from sidae_secretary import cli
from sidae_secretary.config import load_settings


def _existing_settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        storage_root_dir=None,
        timezone="Asia/Seoul",
        uclass_ws_base="",
        uclass_wstoken="",
        telegram_enabled=False,
        telegram_allowed_chat_ids=[],
        telegram_bot_token=None,
        llm_enabled=False,
        llm_provider="local",
        llm_model="gemma4",
        database_path=tmp_path / "sidae.db",
    )


def test_init_writes_storage_root_setting(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    config_file = tmp_path / "config.toml"
    env_file = tmp_path / ".env"

    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda config_file=None: _existing_settings(tmp_path),
    )

    result = runner.invoke(
        cli.app,
        [
            "init",
            "--config-file",
            str(config_file),
            "--env-file",
            str(env_file),
            "--force",
        ],
        input="\n".join(
            [
                str(tmp_path / "storage"),
                "Asia/Seoul",
                "",
                "",
                "n",
                "n",
                "n",
            ]
        )
        + "\n",
    )

    assert result.exit_code == 0
    payload = config_file.read_text(encoding="utf-8")
    assert f'STORAGE_ROOT_DIR = "{tmp_path / "storage"}"' in payload
    assert 'TIMEZONE = "Asia/Seoul"' in payload


def test_storage_root_defaults_apply_when_keys_missing(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[sidae]",
                'TIMEZONE = "Asia/Seoul"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STORAGE_ROOT_DIR", raising=False)

    settings = load_settings(config_file=config_file)

    assert settings.storage_root_dir is None
