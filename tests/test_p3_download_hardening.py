from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

from sidae_secretary.db import Database
from sidae_secretary.jobs import pipeline


def test_append_token_to_url_preserves_existing_params() -> None:
    url = "https://uclass.example/pluginfile.php/10/file.pdf?forcedownload=1&a=1&a=2"
    out = pipeline._append_token_to_url(url, "abc123")
    parsed = urlparse(out)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    assert ("forcedownload", "1") in query_items
    assert query_items.count(("a", "1")) == 1
    assert query_items.count(("a", "2")) == 1
    assert ("token", "abc123") in query_items


def test_resolve_download_filename_uses_content_disposition_and_mime() -> None:
    name = pipeline._resolve_download_filename(
        url="https://example.com/pluginfile.php/77/resource",
        content_disposition='attachment; filename="week1"',
        content_type="application/pdf",
        fallback_name="resource",
    )
    assert name == "week1.pdf"


def test_filename_from_content_disposition_repairs_utf8_mojibake() -> None:
    repaired = pipeline._filename_from_content_disposition(
        'attachment; filename="í\x98\x84ë\x8c\x80ë³µì§\x80ì\x82¬í\x9a\x8cì\x99\x80 ë²\x95 1ì£¼ì°¨.pptx"'
    )
    assert repaired == "현대복지사회와_법_1주차.pptx"


def test_download_avoids_overwrite_on_filename_collision(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    target = tmp_path / "materials" / "course" / "2026-03-05" / "slides.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old-content")

    monkeypatch.setattr(
        pipeline,
        "_download_material_response",
        lambda url, token, retries, backoff_sec: (
            b"new-content",
            {
                "content-disposition": 'attachment; filename="slides.pdf"',
                "content-type": "application/pdf",
            },
            url,
        ),
    )

    settings = SimpleNamespace(
        uclass_wstoken="token",
        uclass_download_retries=1,
        uclass_download_backoff_sec=0.01,
    )

    path1, digest1, downloaded1, meta1 = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:a1",
        url="https://example.com/pluginfile.php?id=1",
        target=target,
        owner_id=0,
    )
    assert downloaded1 is True
    assert Path(path1).name == "slides_1.pdf"
    assert meta1["resolved_filename"] == "slides_1.pdf"
    assert target.read_bytes() == b"old-content"

    path2, digest2, downloaded2, _ = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:a2",
        url="https://example.com/pluginfile.php?id=2",
        target=target,
        owner_id=0,
    )
    assert downloaded2 is False
    assert path2 == path1
    assert digest2 == digest1


def test_download_material_rejects_json_error_payload(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    target = tmp_path / "materials" / "course" / "2026-03-05" / "slides.hwp"

    monkeypatch.setattr(
        pipeline,
        "_download_material_response",
        lambda url, token, retries, backoff_sec: (
            b'{"error":"\xed\x95\x84\xec\x88\x98 \xeb\xa7\xa4\xea\xb0\x9c\xeb\xb3\x80\xec\x88\x98 (token) \xeb\x88\x84\xeb\x9d\xbd","errorcode":"missingparam"}',
            {
                "content-disposition": 'attachment; filename="slides.hwp"',
                "content-type": "application/json; charset=utf-8",
            },
            url,
        ),
    )

    settings = SimpleNamespace(
        uclass_wstoken="token",
        uclass_username="",
        uclass_password="",
        uclass_download_retries=1,
        uclass_download_backoff_sec=0.01,
    )

    try:
        pipeline._download_material(
            db=db,
            settings=settings,
            external_id="uclass:artifact:json-error",
            url="https://example.com/pluginfile.php?id=3",
            target=target,
            owner_id=0,
        )
    except RuntimeError as exc:
        assert "missingparam" in str(exc)
    else:
        raise AssertionError("json error payload should raise")

    assert not target.exists()


def test_download_material_retries_with_fresh_token_after_missingparam_json(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    target = tmp_path / "materials" / "course" / "2026-03-05" / "slides.pdf"
    calls: list[str] = []

    def _fake_download(url, token, retries, backoff_sec):
        calls.append(str(token))
        if len(calls) == 1:
            return (
                b'{"error":"token missing","errorcode":"missingparam"}',
                {
                    "content-disposition": 'attachment; filename="slides.pdf"',
                    "content-type": "application/json; charset=utf-8",
                },
                url,
            )
        return (
            b"%PDF-1.4 fresh",
            {
                "content-disposition": 'attachment; filename="slides.pdf"',
                "content-type": "application/pdf",
            },
            url,
        )

    monkeypatch.setattr(pipeline, "_download_material_response", _fake_download)
    monkeypatch.setattr(
        pipeline,
        "_resolve_uclass_token",
        lambda settings, prefer_static=True, **kwargs: "fresh-token",
    )

    settings = SimpleNamespace(
        uclass_wstoken="stale-token",
        uclass_username="student",
        uclass_password="secret",
        uclass_download_retries=1,
        uclass_download_backoff_sec=0.01,
    )

    path, digest, downloaded, metadata = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:retry-json",
        url="https://example.com/pluginfile.php?id=4",
        target=target,
        owner_id=0,
    )

    assert calls == ["stale-token", "fresh-token"]
    assert downloaded is True
    assert digest
    assert Path(path).read_bytes() == b"%PDF-1.4 fresh"
    assert metadata["content_type"] == "application/pdf"


def test_download_material_redownloads_invalid_existing_json_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "sidae.db")
    db.init()
    target = tmp_path / "materials" / "course" / "2026-03-05" / "slides.hwp"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '{"error":"필수 매개변수 (token) 누락","errorcode":"missingparam"}',
        encoding="utf-8",
    )
    db.record_artifact(
        external_id="uclass:artifact:broken-json",
        source="uclass",
        filename="slides.hwp",
        icloud_path=str(target),
        content_hash="broken",
        metadata_json={"content_type": "application/json; charset=utf-8"},
    )

    monkeypatch.setattr(
        pipeline,
        "_download_material_response",
        lambda url, token, retries, backoff_sec: (
            b"%PDF-1.4 repaired",
            {
                "content-disposition": 'attachment; filename="slides.hwp"',
                "content-type": "application/octet-stream",
            },
            url,
        ),
    )

    settings = SimpleNamespace(
        uclass_wstoken="token",
        uclass_username="",
        uclass_password="",
        uclass_download_retries=1,
        uclass_download_backoff_sec=0.01,
    )

    path, digest, downloaded, _ = pipeline._download_material(
        db=db,
        settings=settings,
        external_id="uclass:artifact:broken-json",
        url="https://example.com/pluginfile.php?id=5",
        target=target,
        owner_id=0,
    )

    assert Path(path) == target
    assert downloaded is True
    assert digest
    assert target.read_bytes() == b"%PDF-1.4 repaired"
