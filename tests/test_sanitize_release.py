from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sanitize_release.py"
SPEC = importlib.util.spec_from_file_location("sanitize_release", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
sanitize_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sanitize_release
SPEC.loader.exec_module(sanitize_release)


def test_classify_path_catches_required_runtime_artifacts() -> None:
    cases = (
        (Path(".env"), False, "environment file"),
        (Path(".DS_Store"), False, "local desktop artifact"),
        (Path(".coverage"), False, "tooling cache"),
        (Path("config.toml"), False, "runtime config file"),
        (Path(".codex"), True, "local Codex environment metadata"),
        (Path(".venv"), True, "virtual environment"),
        (Path(".cache"), True, "cache directory"),
        (Path("node_modules"), True, "tooling cache"),
        (Path("src/package.egg-info"), True, "Python package metadata artifact"),
        (Path("data"), True, "runtime data directory"),
        (Path("credentials"), True, "credentials directory"),
        (Path("secret_store"), True, "secret-store directory"),
        (Path("data/chatgpt_web_profile"), True, "browser profile artifact"),
        (Path("data/onboarding_browser_profiles"), True, "browser profile artifact"),
        (Path("data/chromium_profile"), True, "browser profile artifact"),
        (Path("profile/Cookies"), False, "browser profile artifact"),
        (Path("profile/Login Data"), False, "browser profile artifact"),
        (Path("profile/Local Storage"), True, "browser profile artifact"),
        (Path("profile/IndexedDB"), True, "browser profile artifact"),
        (Path("foo/sidae.db"), False, "sqlite database file"),
        (Path("foo/runtime.lock"), False, "runtime lock file"),
        (Path(".pytest_cache"), True, "pytest cache"),
        (Path("export-before-mini.json"), False, "export dump"),
        (Path("dump-before-public.sql"), False, "export dump"),
    )

    for path, is_dir, expected in cases:
        reason = sanitize_release.classify_path(path, is_dir=is_dir)
        assert reason == expected


def test_create_staging_dir_skips_runtime_artifacts(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    staging_dir = tmp_path / "stage"

    (source_root / "src").mkdir(parents=True)
    (source_root / "src" / "package.egg-info").mkdir()
    (source_root / "src" / "package.egg-info" / "PKG-INFO").write_text("name\n", encoding="utf-8")
    (source_root / ".cache").mkdir()
    (source_root / ".cache" / "tool.tmp").write_text("tmp\n", encoding="utf-8")
    (source_root / ".codex").mkdir()
    (source_root / ".codex" / "environment.toml").write_text("command = 'private'\n", encoding="utf-8")
    (source_root / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source_root / "README.md").write_text("# repo\n", encoding="utf-8")
    (source_root / ".DS_Store").write_text("desktop", encoding="utf-8")
    (source_root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (source_root / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (source_root / "config.toml").write_text("INSTANCE_NAME = 'beta'\n", encoding="utf-8")
    (source_root / "data").mkdir()
    (source_root / "data" / "sidae.db").write_text("db", encoding="utf-8")
    (source_root / "credentials").mkdir()
    (source_root / "credentials" / "secret.sample").write_text("{}", encoding="utf-8")
    (source_root / "secret_store").mkdir()
    (source_root / "secret_store" / "secret.json").write_text("{}", encoding="utf-8")
    (source_root / "chromium_profile").mkdir()
    (source_root / "chromium_profile" / "Cookies").write_text("cookie", encoding="utf-8")
    (source_root / "data" / "chatgpt_web_profile").mkdir()
    (source_root / "data" / "chatgpt_web_profile" / "Cookies").write_text("cookie", encoding="utf-8")
    (source_root / "browser").mkdir()
    (source_root / "browser" / "Login Data").write_text("login", encoding="utf-8")
    (source_root / "browser" / "IndexedDB").mkdir()
    (source_root / "runtime.lock").write_text("", encoding="utf-8")
    (source_root / "dump-before-public.sql").write_text("dump", encoding="utf-8")
    (source_root / "poetry.lock").write_text("keep", encoding="utf-8")

    report = sanitize_release.create_staging_dir(source_root, staging_dir)

    assert (staging_dir / "README.md").exists()
    assert (staging_dir / "src" / "main.py").exists()
    assert (staging_dir / ".env.example").exists()
    assert (staging_dir / "poetry.lock").exists()
    assert not (staging_dir / ".env").exists()
    assert not (staging_dir / ".DS_Store").exists()
    assert not (staging_dir / ".cache").exists()
    assert not (staging_dir / ".codex").exists()
    assert not (staging_dir / "src" / "package.egg-info").exists()
    assert not (staging_dir / "config.toml").exists()
    assert not (staging_dir / "data").exists()
    assert not (staging_dir / "credentials").exists()
    assert not (staging_dir / "secret_store").exists()
    assert not (staging_dir / "chromium_profile").exists()
    assert not (staging_dir / "data" / "chatgpt_web_profile").exists()
    assert not (staging_dir / "browser" / "Login Data").exists()
    assert not (staging_dir / "browser" / "IndexedDB").exists()
    assert not (staging_dir / "runtime.lock").exists()
    assert not (staging_dir / "dump-before-public.sql").exists()
    assert any(item.path == Path(".env") for item in report.excluded_paths)


def test_validate_staging_dir_reports_runtime_state(tmp_path: Path) -> None:
    staging_dir = tmp_path / "stage"
    staging_dir.mkdir()
    (staging_dir / "README.md").write_text("# repo\n", encoding="utf-8")
    (staging_dir / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (staging_dir / "config.toml").write_text("INSTANCE_NAME = 'beta'\n", encoding="utf-8")
    (staging_dir / "data").mkdir()
    (staging_dir / "data" / "sidae.db").write_text("db", encoding="utf-8")

    violations = sanitize_release.validate_staging_dir(staging_dir)

    reasons = {(item.path.as_posix(), item.reason) for item in violations}
    assert (".env", "environment file") in reasons
    assert ("config.toml", "runtime config file") in reasons
    assert ("data", "runtime data directory") in reasons


def test_validate_staging_dir_reports_sensitive_text_content(tmp_path: Path) -> None:
    staging_dir = tmp_path / "stage"
    staging_dir.mkdir()
    private_user = "j_" + "mac"
    private_path = "/" + "Users" + f"/{private_user}/Desktop/" + "UOS_" + "secretary"
    deploy_command = "git push " + "mini" + " HEAD:deploy"
    public_url = "https://connect.example" + ".com/moodle-connect"
    student_id = "2026" + "0001"
    bot_token = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    (staging_dir / "README.md").write_text(
        "\n".join(
            [
                f"private path {private_path}",
                f"deploy command {deploy_command}",
                f"public url {public_url}",
                f"student source_url https://api.example.invalid/timetable/{student_id}",
                f"bot token {bot_token}",
            ]
        ),
        encoding="utf-8",
    )

    violations = sanitize_release.validate_staging_dir(staging_dir)

    reasons = {item.reason for item in violations}
    assert "internal local user path" in reasons
    assert "private deployment remote" in reasons
    assert "realistic public onboarding URL fixture" in reasons
    assert "student-number-like fixture" in reasons
    assert "Telegram bot token" in reasons


def test_create_staging_dir_rejects_nested_output(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    nested_staging_dir = source_root / "stage"

    try:
        sanitize_release.create_staging_dir(source_root, nested_staging_dir)
    except ValueError as exc:
        assert "excluded directory" in str(exc)
    else:
        raise AssertionError("expected nested staging directory to be rejected")


def test_create_staging_dir_allows_dist_output_inside_repo(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    staging_dir = source_root / "dist" / "release"

    source_root.mkdir()
    (source_root / "README.md").write_text("# repo\n", encoding="utf-8")

    sanitize_release.create_staging_dir(source_root, staging_dir)

    assert (staging_dir / "README.md").exists()
