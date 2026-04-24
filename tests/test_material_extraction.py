from __future__ import annotations

import struct
import sys
import types
import zlib
from pathlib import Path

from sidae_secretary.jobs import pipeline


def _hwp_record(tag_id: int, payload: bytes) -> bytes:
    header = int(tag_id) | (len(payload) << 20)
    return struct.pack("<I", header) + payload


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


def test_extract_material_text_from_php_html_file(tmp_path: Path) -> None:
    path = tmp_path / "view.php"
    path.write_text(
        """
        <html>
          <head>
            <title>Algorithms 공지사항</title>
            <script>window.should_not_appear = true;</script>
          </head>
          <body>
            <div class="article-content">
              <p>1주차 안내</p>
              <p>과제는 없습니다.</p>
            </div>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    text, error, extract_type = pipeline.extract_material_text(path, max_chars=300)

    assert extract_type == "html"
    assert error is None
    assert "Algorithms 공지사항" in str(text)
    assert "1주차 안내" in str(text)
    assert "과제는 없습니다." in str(text)
    assert "should_not_appear" not in str(text)


def test_material_error_message_from_payload_flags_uclass_login_page() -> None:
    payload = """
    <html>
      <head><title>서울시립대학교 온라인강의실</title></head>
      <body>
        <form action="/login/index.php" method="post">
          <input type="text" name="username" />
          <input type="password" name="password" />
          <input type="hidden" name="logintoken" value="abc123" />
          <button type="submit">로그인</button>
        </form>
      </body>
    </html>
    """.encode("utf-8")

    error = pipeline._material_error_message_from_payload(
        payload,
        content_type="text/html; charset=utf-8",
        resolved_url="https://uclass.example/login/index.php",
    )

    assert error == "uclass returned login page html instead of material content"


def test_extract_text_from_hwp_prvtext_bytes_decodes_utf16le() -> None:
    text = pipeline._extract_text_from_hwp_prvtext_bytes(
        "2026학년도 대학글쓰기 강의계획서".encode("utf-16le"),
        max_chars=200,
    )

    assert "2026학년도" in text
    assert "대학글쓰기" in text


def test_extract_text_from_hwp_body_bytes_reads_para_text_records() -> None:
    record_noise = _hwp_record(pipeline.HWP_PARA_TEXT_TAG, "dces".encode("utf-16le"))
    record_text = _hwp_record(
        pipeline.HWP_PARA_TEXT_TAG,
        "수업 목표와 평가 방식을 설명한다.".encode("utf-16le"),
    )

    text = pipeline._extract_text_from_hwp_body_bytes(
        _raw_deflate(record_noise + record_text),
        compressed=True,
        max_chars=200,
    )

    assert "수업 목표와 평가 방식" in text
    assert "dces" not in text


def test_extract_text_from_hwp_uses_preview_stream_when_available(monkeypatch) -> None:
    class _Stream:
        def __init__(self, data: bytes):
            self._data = data

        def read(self) -> bytes:
            return self._data

    class _FakeOle:
        def __enter__(self) -> "_FakeOle":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def exists(self, name: str) -> bool:
            return name in {"PrvText", "FileHeader"}

        def openstream(self, name: str) -> _Stream:
            if name == "PrvText":
                return _Stream("교과목 개요와 운영 방침".encode("utf-16le"))
            if name == "FileHeader":
                header = bytearray(40)
                header[:32] = b"HWP Document File" + b"\x00" * (32 - len("HWP Document File"))
                header[36:40] = struct.pack("<I", 1)
                return _Stream(bytes(header))
            raise KeyError(name)

        def listdir(self):
            return [["PrvText"], ["FileHeader"]]

    fake_module = types.SimpleNamespace(OleFileIO=lambda _: _FakeOle())
    monkeypatch.setitem(sys.modules, "olefile", fake_module)

    text, error = pipeline._extract_text_from_hwp(Path("dummy.hwp"), max_chars=200)

    assert error is None
    assert "교과목 개요" in str(text)
