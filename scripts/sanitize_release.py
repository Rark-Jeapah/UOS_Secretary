#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


ENV_EXAMPLE_NAMES = {".env.example"}
EXACT_RUNTIME_FILE_REASONS = {
    "config.toml": "runtime config file",
}
EXCLUDED_DIR_NAMES = {
    ".codex",
    ".cache",
    ".git",
    ".venv",
    "venv",
    "cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "__pycache__",
    "__pypackages__",
    "build",
    "coverage",
    "dist",
    "htmlcov",
    "node_modules",
    "release-staging",
    "data",
    ".credentials",
    "credential",
    "credentials",
    "secret-store",
    "secret_store",
}
BROWSER_PROFILE_DIR_NAMES = {
    "browser_profile",
    "browser-profile",
    "browser-profiles",
    "chrome_profile",
    "chrome-profile",
    "chatgpt_web_profile",
    "chromium_profile",
    "chromium-profile",
    "onboarding_browser_profiles",
    "playwright_profile",
    "playwright-profile",
    "user-data-dir",
    "user_data_dir",
    "local storage",
    "indexeddb",
}
EXACT_RUNTIME_LOCK_NAMES = {"lock", "singletonlock"}
EXACT_LOCAL_ARTIFACT_FILE_REASONS = {
    ".coverage": "tooling cache",
    ".ds_store": "local desktop artifact",
}
EXACT_BROWSER_PROFILE_FILE_NAMES = {"cookies", "login data", "login data for account"}
SOURCE_LOCKFILE_ALLOWLIST = {
    "cargo.lock",
    "gemfile.lock",
    "package-lock.json",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
EXPORT_DUMP_PATTERNS = (
    "backup-*.zip",
    "backup_*.zip",
    "dump-*.sql",
    "dump_*.sql",
    "export-*.json",
    "export_*.json",
    "export-*.ndjson",
    "export_*.ndjson",
    "export-*.zip",
    "export_*.zip",
)
SQLITE_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
TEXT_CONTENT_SUFFIXES = {
    "",
    ".cfg",
    ".command",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".plist",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MAX_CONTENT_SCAN_BYTES = 2_000_000
PRIVATE_LOCAL_USERS = ("j_" + "mac", "j_" + "home")
PRIVATE_LOCAL_USERS_PATTERN = "|".join(re.escape(item) for item in PRIVATE_LOCAL_USERS)
USER_ROOT_PATTERN = "/" + "Users/"
REPO_NAME_PATTERN = "UOS_" + "secretary"
PRIVATE_MESH_HOST_PREFIX = "tail" + "eed"
PRIVATE_KEY_PATTERN = "-" * 5 + r"BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY" + "-" * 5
CONTENT_BLOCK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            rf"{re.escape(USER_ROOT_PATTERN)}(?:{PRIVATE_LOCAL_USERS_PATTERN})\b"
            rf"|{re.escape(USER_ROOT_PATTERN)}[^/\s]+/(?:Desktop/)?{REPO_NAME_PATTERN}(?:_beta)?"
        ),
        "internal local user path",
    ),
    (
        re.compile(rf"~/apps/{REPO_NAME_PATTERN}(?:_beta)?|~/git/{REPO_NAME_PATTERN}\.git"),
        "private deployment path",
    ),
    (re.compile(rf"\b(?:{PRIVATE_LOCAL_USERS_PATTERN})\b"), "private local username"),
    (re.compile(r"\bmini\.local\b"), "private deployment host"),
    (
        re.compile(r"\bgit\s+(?:push|remote\s+add)\s+mini\b|\bgit\s+ls-remote\s+--heads\s+mini\b"),
        "private deployment remote",
    ),
    (
        re.compile(rf"\b[a-z0-9-]+\.ts\.net\b|\b{PRIVATE_MESH_HOST_PREFIX}[a-z0-9-]*\b", re.IGNORECASE),
        "private mesh host",
    ),
    (re.compile(r"https://connect\.example\.com\b"), "realistic public onboarding URL fixture"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "Telegram bot token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API key token"),
    (
        re.compile(PRIVATE_KEY_PATTERN),
        "private key material",
    ),
    (
        re.compile(r"(?:student|username|학번|student_id|source_url|/timetable/)[^\n]{0,80}\b20\d{6}\b", re.IGNORECASE),
        "student-number-like fixture",
    ),
    (re.compile(r"telegram:\d{9,}:"), "realistic Telegram chat id fixture"),
    (
        re.compile(r"(?:chat_id|TELEGRAM_ALLOWED_CHAT_IDS|sent_to|\"chat\"\s*:\s*\{\s*\"id\")[^\n]{0,80}\b\d{9,12}\b"),
        "realistic Telegram chat id fixture",
    ),
)


@dataclass(frozen=True)
class Violation:
    path: Path
    reason: str


@dataclass(frozen=True)
class StagingReport:
    copied_files: int
    excluded_paths: tuple[Violation, ...]


def default_source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def classify_path(relative_path: Path, *, is_dir: bool) -> str | None:
    if not relative_path.parts:
        return None

    path_lower = relative_path.as_posix().lower()
    name = relative_path.name
    name_lower = name.lower()
    dir_names_lower = {part.lower() for part in relative_path.parts[:-1] if part not in {".", ""}}

    if is_dir:
        if name in ENV_EXAMPLE_NAMES:
            return None
        if name_lower.endswith(".egg-info"):
            return "Python package metadata artifact"
        if name_lower in EXCLUDED_DIR_NAMES:
            return _reason_for_directory(name_lower)
        if name_lower in BROWSER_PROFILE_DIR_NAMES:
            return "browser profile artifact"
        return None

    if name in ENV_EXAMPLE_NAMES:
        return None
    if name_lower in EXACT_LOCAL_ARTIFACT_FILE_REASONS:
        return EXACT_LOCAL_ARTIFACT_FILE_REASONS[name_lower]
    if name_lower in EXACT_RUNTIME_FILE_REASONS:
        return EXACT_RUNTIME_FILE_REASONS[name_lower]
    if name == ".env" or (name.startswith(".env.") and name not in ENV_EXAMPLE_NAMES):
        return "environment file"
    if dir_names_lower & {".pytest_cache"}:
        return "pytest cache"
    if dir_names_lower & {"__pycache__"}:
        return "Python cache"
    if dir_names_lower & BROWSER_PROFILE_DIR_NAMES:
        return "browser profile artifact"
    if dir_names_lower & {"data"}:
        return "runtime data directory"
    if dir_names_lower & {".credentials", "credential", "credentials"}:
        return "credentials directory"
    if dir_names_lower & {"secret-store", "secret_store"}:
        return "secret-store directory"
    if name_lower in EXACT_BROWSER_PROFILE_FILE_NAMES:
        return "browser profile artifact"
    if any(path_lower.endswith(pattern) for pattern in SQLITE_SUFFIXES):
        return "sqlite database file"
    if name_lower in EXACT_RUNTIME_LOCK_NAMES:
        return "runtime lock file"
    if name_lower.endswith((".lock", ".lck")) and name_lower not in SOURCE_LOCKFILE_ALLOWLIST:
        return "runtime lock file"
    if dir_names_lower & {part for part in dir_names_lower if part.endswith(".egg-info")}:
        return "Python package metadata artifact"
    if _matches_export_dump(name_lower):
        return "export dump"
    return None


def _reason_for_directory(name_lower: str) -> str:
    if name_lower == ".codex":
        return "local Codex environment metadata"
    if name_lower == ".git":
        return "git metadata"
    if name_lower in {".venv", "venv"}:
        return "virtual environment"
    if name_lower == ".pytest_cache":
        return "pytest cache"
    if name_lower in {".cache", "cache"}:
        return "cache directory"
    if name_lower in {
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "__pycache__",
        "__pypackages__",
        "coverage",
        "htmlcov",
        "node_modules",
    }:
        return "tooling cache"
    if name_lower in {"build", "dist", "release-staging"}:
        return "release build artifact"
    if name_lower == "data":
        return "runtime data directory"
    if name_lower in {".credentials", "credential", "credentials"}:
        return "credentials directory"
    if name_lower in {"secret-store", "secret_store"}:
        return "secret-store directory"
    return "excluded directory"


def _matches_export_dump(name_lower: str) -> bool:
    if name_lower == "export.json":
        return True
    for pattern in EXPORT_DUMP_PATTERNS:
        if Path(name_lower).match(pattern):
            return True
    return False


def validate_staging_dir(staging_dir: Path) -> list[Violation]:
    staging_root = staging_dir.resolve()
    if not staging_root.is_dir():
        raise ValueError(f"staging directory does not exist: {staging_root}")
    violations: list[Violation] = []
    for root, dirnames, filenames in _walk_sorted(staging_root):
        current_root = Path(root)
        rel_root = current_root.relative_to(staging_root)

        kept_dirnames: list[str] = []
        for dirname in dirnames:
            rel_path = rel_root / dirname if rel_root != Path(".") else Path(dirname)
            reason = classify_path(rel_path, is_dir=True)
            if reason is not None:
                violations.append(Violation(path=rel_path, reason=reason))
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in filenames:
            rel_path = rel_root / filename if rel_root != Path(".") else Path(filename)
            reason = classify_path(rel_path, is_dir=False)
            if reason is not None:
                violations.append(Violation(path=rel_path, reason=reason))
                continue
            violations.extend(_validate_file_content(current_root / filename, rel_path))
    return violations


def _validate_file_content(path: Path, relative_path: Path) -> list[Violation]:
    if not _should_scan_file_content(relative_path):
        return []
    try:
        if path.stat().st_size > MAX_CONTENT_SCAN_BYTES:
            return []
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    violations: list[Violation] = []
    for pattern, reason in CONTENT_BLOCK_PATTERNS:
        if pattern.search(content):
            violations.append(Violation(path=relative_path, reason=reason))
    return violations


def _should_scan_file_content(relative_path: Path) -> bool:
    name_lower = relative_path.name.lower()
    if name_lower in {".env.example", "dockerfile", "makefile"}:
        return True
    return relative_path.suffix.lower() in TEXT_CONTENT_SUFFIXES


def create_staging_dir(source_root: Path, staging_dir: Path, *, force: bool = False) -> StagingReport:
    source_root = source_root.resolve()
    staging_dir = staging_dir.resolve()
    _ensure_staging_target(source_root, staging_dir, force=force)

    excluded_paths: list[Violation] = []
    copied_files = 0

    for root, dirnames, filenames in _walk_sorted(source_root):
        current_root = Path(root)
        rel_root = current_root.relative_to(source_root)

        kept_dirnames: list[str] = []
        for dirname in dirnames:
            rel_path = rel_root / dirname if rel_root != Path(".") else Path(dirname)
            reason = classify_path(rel_path, is_dir=True)
            if reason is not None:
                excluded_paths.append(Violation(path=rel_path, reason=reason))
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in filenames:
            rel_path = rel_root / filename if rel_root != Path(".") else Path(filename)
            reason = classify_path(rel_path, is_dir=False)
            if reason is not None:
                excluded_paths.append(Violation(path=rel_path, reason=reason))
                continue
            destination = staging_dir / rel_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current_root / filename, destination)
            copied_files += 1

    return StagingReport(copied_files=copied_files, excluded_paths=tuple(excluded_paths))


def _ensure_staging_target(source_root: Path, staging_dir: Path, *, force: bool) -> None:
    if staging_dir == source_root:
        raise ValueError("staging directory must not be the source tree")
    if source_root in staging_dir.parents and not _is_pruned_from_source_walk(source_root, staging_dir):
        raise ValueError(
            "staging directory inside the source tree must live under an excluded directory such as dist/"
        )

    if staging_dir.exists():
        if not force:
            raise ValueError(f"staging directory already exists: {staging_dir}")
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir)
        else:
            staging_dir.unlink()
    staging_dir.mkdir(parents=True, exist_ok=True)


def _is_pruned_from_source_walk(source_root: Path, staging_dir: Path) -> bool:
    relative_path = staging_dir.relative_to(source_root)
    current = Path()
    for part in relative_path.parts:
        current = current / part
        if classify_path(current, is_dir=True) is not None:
            return True
    return False


def _walk_sorted(root: Path):
    for current_root, dirnames, filenames in os.walk(root, topdown=True):
        dirnames.sort()
        filenames.sort()
        yield current_root, dirnames, filenames


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or validate a sanitized release staging directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="create a clean staging directory")
    create_parser.add_argument("staging_dir", type=Path, help="output directory for the sanitized stage")
    create_parser.add_argument(
        "--source",
        type=Path,
        default=default_source_root(),
        help="repository root to sanitize (defaults to this repo)",
    )
    create_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing staging directory",
    )

    validate_parser = subparsers.add_parser("validate", help="validate an existing staging directory")
    validate_parser.add_argument("staging_dir", type=Path, help="directory to validate")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            report = create_staging_dir(args.source, args.staging_dir, force=args.force)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"created sanitized staging directory: {args.staging_dir.resolve()}")
        print(f"copied files: {report.copied_files}")
        print(f"excluded paths: {len(report.excluded_paths)}")
        return 0

    try:
        violations = validate_staging_dir(args.staging_dir)
    except ValueError as exc:
        parser.error(str(exc))
    if violations:
        print(f"release staging validation failed: {args.staging_dir.resolve()}", file=sys.stderr)
        for violation in violations:
            print(f"- {violation.path.as_posix()}: {violation.reason}", file=sys.stderr)
        return 1

    print(f"release staging validation passed: {args.staging_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
