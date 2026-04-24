from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def test_download_material_skips_when_existing_record_and_file_present(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    existing_path = tmp_path / "materials" / "course" / "2026-03-04" / "file.pdf"
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_bytes(b"same-bytes")

    db.record_artifact(
        external_id="uclass:artifact:test",
        source="uclass",
        filename="file.pdf",
        icloud_path=str(existing_path),
        content_hash="abc123",
        metadata_json={"url": "https://example.com/file.pdf"},
    )

    def _should_not_download(*args, **kwargs):
        raise AssertionError("download should be skipped")

    monkeypatch.setattr(pipeline, "_download_material_response", _should_not_download)

    settings = SimpleNamespace(
        uclass_wstoken="token",
        uclass_download_retries=3,
        uclass_download_backoff_sec=0.1,
    )
    path, digest, downloaded, metadata = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:test",
        url="https://example.com/file.pdf",
        target=existing_path,
        owner_id=0,
    )

    assert path == str(existing_path)
    assert digest == "abc123"
    assert downloaded is False
    assert metadata["resolved_filename"] == "file.pdf"


def test_download_material_uses_owner_scoped_artifact_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()

    existing_path = tmp_path / "materials" / "course" / "2026-03-04" / "file.pdf"
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_bytes(b"same-bytes")

    db.record_artifact(
        external_id="uclass:artifact:test-owner",
        source="uclass",
        filename="file.pdf",
        icloud_path=str(existing_path),
        content_hash="owner-hash",
        metadata_json={"url": "https://example.com/file.pdf"},
        user_id=7,
    )

    def _should_not_download(*args, **kwargs):
        raise AssertionError("download should be skipped for the matching owner")

    monkeypatch.setattr(pipeline, "_download_material_response", _should_not_download)

    settings = SimpleNamespace(
        uclass_wstoken="token",
        uclass_download_retries=3,
        uclass_download_backoff_sec=0.1,
    )
    path, digest, downloaded, metadata = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:test-owner",
        url="https://example.com/file.pdf",
        target=existing_path,
        owner_id=7,
    )

    assert path == str(existing_path)
    assert digest == "owner-hash"
    assert downloaded is False
    assert metadata["resolved_filename"] == "file.pdf"
