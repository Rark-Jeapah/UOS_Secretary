from __future__ import annotations

import pytest
import requests

from sidae_secretary.connectors import portal

pytestmark = pytest.mark.beta_critical


def test_fetch_uos_notice_feed_parses_unique_titles_and_metadata(monkeypatch) -> None:
    html = """
    <html><body>
      <ul class="brd-lstp1">
        <li>
          <div>
            <div class="ti">
              <a href="javascript:fnView('1', '30524');">
                2026년 대학생 청소년교육지원장학금 활동도우미(근로) 모집
              </a>
            </div>
            <div class="da">
              <span>대외협력과</span>
              <span>2026-03-06</span>
            </div>
          </div>
        </li>
        <li>
          <div>
            <div class="ti">
              <a href="javascript:fnView('2', '30525');">
                [인터넷증명발급] (신)인터넷 증명발급 시스템 오픈 및 원본대조 서비스 이용 안내
              </a>
            </div>
            <div class="da">
              <span>전산정보과</span>
              <span>2026-03-06</span>
            </div>
          </div>
        </li>
        <li>
          <div>
            <div class="ti">
              <a href="javascript:fnView('1', '30524');">
                2026년 대학생 청소년교육지원장학금 활동도우미(근로) 모집
              </a>
            </div>
            <div class="da">
              <span>대외협력과</span>
              <span>2026-03-06</span>
            </div>
          </div>
        </li>
      </ul>
    </body></html>
    """

    class _FakeResponse:
        text = html
        url = "https://www.uos.ac.kr/korNotice/list.do?list_id=FA1&menuid=2000005009002000000"
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    def _fake_get(url: str, params=None, headers=None, timeout=None):
        return _FakeResponse()

    monkeypatch.setattr(portal.requests, "get", _fake_get)

    result = portal.fetch_uos_notice_feed(
        list_id="FA1",
        menuid="2000005009002000000",
        limit=10,
    )

    notices = result.notices
    assert len(notices) == 2
    assert notices[0].seq == "30524"
    assert notices[0].title == "2026년 대학생 청소년교육지원장학금 활동도우미(근로) 모집"
    assert notices[0].article_url is not None
    assert notices[1].department == "전산정보과"
    assert notices[1].posted_on == "2026-03-06"
    assert result.metadata.list_id == "FA1"
    assert result.metadata.menuid == "2000005009002000000"
    assert result.metadata.parsed_count == 2
    assert result.metadata.empty_detected is False
    assert result.metadata.source_url.endswith("list_id=FA1&menuid=2000005009002000000&pageIndex=1&searchCnd=1&searchWrd=&viewAuth=Y&writeAuth=N")


def test_fetch_uos_notice_feed_treats_empty_page_as_valid_empty_result(monkeypatch) -> None:
    html = """
    <html>
      <head><title>일반공지 &lt; UOS공지</title></head>
      <body>
        <div class="ti">게시글이 없습니다.</div>
      </body>
    </html>
    """

    class _FakeResponse:
        text = html
        url = "https://www.uos.ac.kr/korNotice/list.do?list_id=FA1&menuid=2000005009002000000"
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(portal.requests, "get", lambda *args, **kwargs: _FakeResponse())

    result = portal.fetch_uos_notice_feed(
        list_id="FA1",
        menuid="2000005009002000000",
        limit=10,
    )

    assert result.notices == []
    assert result.metadata.parsed_count == 0
    assert result.metadata.empty_detected is True
    assert result.metadata.page_title == "일반공지 < UOS공지"


def test_fetch_uos_notice_feed_raises_typed_error_with_metadata_on_upstream_failure(monkeypatch) -> None:
    def _fake_get(url: str, params=None, headers=None, timeout=None):
        raise requests.Timeout("portal timeout")

    monkeypatch.setattr(portal.requests, "get", _fake_get)

    with pytest.raises(portal.PortalNoticeFetchError) as exc_info:
        portal.fetch_uos_notice_feed(
            list_id="FA1",
            menuid="2000005009002000000",
            limit=10,
        )

    assert "portal timeout" in str(exc_info.value)
    assert exc_info.value.metadata["list_id"] == "FA1"
    assert exc_info.value.metadata["menuid"] == "2000005009002000000"
    assert exc_info.value.metadata["requested_limit"] == 10
