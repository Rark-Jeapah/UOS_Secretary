from __future__ import annotations

from pathlib import Path

from sidae_secretary.config import load_settings


def test_load_settings_resolves_relative_paths_from_config_directory(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "configs" / "nested"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "custom.toml"
    config_file.write_text(
        "\n".join(
            [
                "[sidae]",
                'DATABASE_PATH = "data/local.db"',
                'STORAGE_ROOT_DIR = "storage-root"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    for key in (
        "DATABASE_PATH",
        "STORAGE_ROOT_DIR",
        "SIDAE_CONFIG_FILE",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = load_settings(config_file=config_file)

    assert settings.database_path == (config_dir / "data" / "local.db").resolve()
    assert settings.storage_root_dir == (config_dir / "storage-root").resolve()


def test_load_settings_normalizes_instance_name(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[sidae]",
                'INSTANCE_NAME = "Beta App"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INSTANCE_NAME", raising=False)

    settings = load_settings(config_file=config_file)

    assert settings.instance_name == "beta-app"


def test_load_settings_parses_onboarding_allowed_school_slugs(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[sidae]",
                'ONBOARDING_ALLOWED_SCHOOL_SLUGS = "uos_online_class, uos_portal"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    settings = load_settings(config_file=config_file)

    assert settings.onboarding_allowed_school_slugs == ["uos_online_class", "uos_portal"]


def test_load_settings_parses_ops_dashboard_remote_access_settings(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[sidae]",
                'OPS_DASHBOARD_SSH_HOST = "runtime.local"',
                'OPS_DASHBOARD_SSH_USER = "runtime_user"',
                "OPS_DASHBOARD_SSH_PORT = 2201",
                'OPS_DASHBOARD_REMOTE_HOST = "127.0.0.1"',
                "OPS_DASHBOARD_REMOTE_PORT = 8794",
                "OPS_DASHBOARD_LOCAL_PORT = 9901",
                'OPS_DASHBOARD_URL_PATH = "healthz"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    settings = load_settings(config_file=config_file)

    assert settings.ops_dashboard_ssh_host == "runtime.local"
    assert settings.ops_dashboard_ssh_user == "runtime_user"
    assert settings.ops_dashboard_ssh_port == 2201
    assert settings.ops_dashboard_remote_host == "127.0.0.1"
    assert settings.ops_dashboard_remote_port == 8794
    assert settings.ops_dashboard_local_port == 9901
    assert settings.ops_dashboard_url_path == "/healthz"


def test_load_settings_defaults_telegram_assistant_flags_to_false(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[sidae]\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_ASSISTANT_ENABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_ASSISTANT_WRITE_ENABLED", raising=False)

    settings = load_settings(config_file=config_file)

    assert settings.telegram_assistant_enabled is False
    assert settings.telegram_assistant_write_enabled is False


def test_load_settings_reads_telegram_assistant_flags_from_config_and_dotenv(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[sidae]",
                "TELEGRAM_ASSISTANT_ENABLED = true",
                "TELEGRAM_ASSISTANT_WRITE_ENABLED = false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "TELEGRAM_ASSISTANT_WRITE_ENABLED=true\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_ASSISTANT_ENABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_ASSISTANT_WRITE_ENABLED", raising=False)

    settings = load_settings(config_file=config_file)

    assert settings.telegram_assistant_enabled is True
    assert settings.telegram_assistant_write_enabled is True
    assert settings.as_doctor_dict()["TELEGRAM_ASSISTANT_ENABLED"] == "True"
    assert settings.as_doctor_dict()["TELEGRAM_ASSISTANT_WRITE_ENABLED"] == "True"
