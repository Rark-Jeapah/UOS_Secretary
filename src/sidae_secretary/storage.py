from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_storage_root(settings: Any) -> Path | None:
    raw = getattr(settings, "storage_root_dir", None)
    if raw is None:
        raw = getattr(settings, "icloud_dir", None)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def dashboard_dir(root: Path) -> Path:
    return root / "publish" / "dashboard"


def materials_dir(root: Path) -> Path:
    return root / "materials"


def backups_dir(root: Path) -> Path:
    return root / "backups"


def browser_profiles_dir(root: Path) -> Path:
    return root / "browser_profiles"


def expected_storage_subdirs(root: Path) -> list[Path]:
    return [
        dashboard_dir(root),
        materials_dir(root),
        backups_dir(root),
        browser_profiles_dir(root),
    ]
