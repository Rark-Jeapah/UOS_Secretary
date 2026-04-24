from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidae_secretary import docs_artifacts


def _write_docs_fixture(root: Path) -> None:
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "snapshot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00Z",
                "repo": {"git": {"branch": "old", "head": "old", "dirty": False}},
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (docs_dir / "audit.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00Z",
                "git": {"branch": "old", "head": "old", "dirty": False},
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (docs_dir / "SNAPSHOT.md").write_text(
        "\n".join(
            [
                "# Snapshot",
                "",
                "Generated at (UTC): `2026-01-01T00:00:00Z`",
                "",
                "- Branch: `old`",
                "- HEAD: `old`",
                "- Working tree dirty: `false`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (docs_dir / "AUDIT.md").write_text(
        "\n".join(
            [
                "# Audit",
                "",
                "Generated at (UTC): `2026-01-01T00:00:00Z`",
                "",
                "- Current HEAD captured: `old` on `old`.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_parse_git_status_porcelain_extracts_paths() -> None:
    output = "\n".join(
        [
            " M docs/audit.json",
            "A  docs/snapshot.json",
            "R  docs/old.md -> docs/new.md",
            "?? docs/extra file.md",
        ]
    )

    parsed = docs_artifacts.parse_git_status_porcelain(output)

    assert parsed == [
        "docs/audit.json",
        "docs/extra file.md",
        "docs/new.md",
        "docs/snapshot.json",
    ]


def test_collect_git_metadata_uses_git_subprocess(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd, check, capture_output, text):
        calls.append(list(cmd))
        tail = cmd[-3:]
        tail2 = cmd[-2:]
        if tail == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(stdout="main\n")
        if tail2 == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout="abc123\n")
        if tail2 == ["status", "--porcelain"]:
            return SimpleNamespace(stdout=" M docs/audit.json\n?? docs/SNAPSHOT.md\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(docs_artifacts.subprocess, "run", _fake_run)

    metadata = docs_artifacts.collect_git_metadata(Path("/tmp/repo"))

    assert metadata.branch == "main"
    assert metadata.head == "abc123"
    assert metadata.dirty is True
    assert metadata.dirty_files == ["docs/SNAPSHOT.md", "docs/audit.json"]
    assert len(calls) == 3


def test_sync_docs_artifacts_keeps_generated_at_and_git_state_consistent(
    tmp_path: Path, monkeypatch
) -> None:
    _write_docs_fixture(tmp_path)
    fake_metadata = docs_artifacts.GitMetadata(
        branch="main",
        head="deadbeef",
        dirty=True,
        dirty_files=["docs/audit.json", "docs/snapshot.json"],
    )
    monkeypatch.setattr(docs_artifacts, "collect_git_metadata", lambda repo_root: fake_metadata)

    result = docs_artifacts.sync_docs_artifacts(
        docs_dir=tmp_path / "docs",
        repo_root=tmp_path,
        generated_at="2026-03-05T12:34:56Z",
    )

    assert result["ok"] is True
    snapshot = json.loads((tmp_path / "docs" / "snapshot.json").read_text(encoding="utf-8"))
    audit = json.loads((tmp_path / "docs" / "audit.json").read_text(encoding="utf-8"))
    snapshot_md = (tmp_path / "docs" / "SNAPSHOT.md").read_text(encoding="utf-8")
    audit_md = (tmp_path / "docs" / "AUDIT.md").read_text(encoding="utf-8")

    assert snapshot["generated_at"] == "2026-03-05T12:34:56Z"
    assert audit["generated_at"] == "2026-03-05T12:34:56Z"
    assert snapshot["repo"]["git"]["head"] == "deadbeef"
    assert snapshot["repo"]["git"]["dirty"] is True
    assert snapshot["repo"]["git"]["dirty_files"] == ["docs/audit.json", "docs/snapshot.json"]
    assert audit["git"]["head"] == "deadbeef"
    assert audit["git"]["dirty"] is True
    assert audit["git"]["dirty_files"] == ["docs/audit.json", "docs/snapshot.json"]
    assert "Generated at (UTC): `2026-03-05T12:34:56Z`" in snapshot_md
    assert "- HEAD: `deadbeef`" in snapshot_md
    assert "- Current HEAD captured: `deadbeef` on `main`." in audit_md


def test_sync_docs_artifacts_require_clean_git_fails_when_dirty(
    tmp_path: Path, monkeypatch
) -> None:
    _write_docs_fixture(tmp_path)
    fake_metadata = docs_artifacts.GitMetadata(
        branch="main",
        head="deadbeef",
        dirty=True,
        dirty_files=["docs/audit.json"],
    )
    monkeypatch.setattr(docs_artifacts, "collect_git_metadata", lambda repo_root: fake_metadata)

    with pytest.raises(RuntimeError):
        docs_artifacts.sync_docs_artifacts(
            docs_dir=tmp_path / "docs",
            repo_root=tmp_path,
            require_clean_git=True,
        )
