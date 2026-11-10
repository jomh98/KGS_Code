"""수집기 파서 테스트 (HTTP 없이 고정 샘플로)."""

from datetime import date

import pytest

from ingest.kgs_crawler import parse_list_page
from ingest.law_api import (
    LawApiError,
    LawApiClient,
    _parse_xml,
    law_url,
    parse_admrul_service,
    parse_law_search,
    parse_law_service,
)


def test_parse_law_search(fixture_bytes):
    rows = parse_law_search(fixture_bytes("law_search.xml"))
    names = [r["name"] for r in rows]
    assert "도시가스사업법 시행규칙" in names
    first = rows[0]
    assert first["mst"] == "900001"
    assert first["effective_date"] == "2025-07-01"


def test_parse_law_service(fixture_bytes):
    rec = parse_law_service(fixture_bytes("law_service.xml"), today=date(2026, 1, 1))
    assert rec["name"] == "도시가스사업법"
    assert rec["source_type"] == "법률"
    labels = [a["label"] for a in rec["articles"]]
    assert labels == ["제1조", "제17조의5", "제30조"]  # '전문'(장 제목) 제외
    assert rec["articles"][2]["status"] == "시행예정"
    assert rec["articles"][0]["status"] == "현행"
    assert rec["appendices"][0]["label"] == "별표 7"


def test_parse_admrul_service(fixture_bytes):
    rec = parse_admrul_service(fixture_bytes("admrul_service.xml"), today=date(2026, 1, 1))
    assert rec["source_type"] == "행정규칙"
    assert [a["label"] for a in rec["articles"]] == ["제1조", "제2조의2"]
    assert rec["effective_date"] == "2025-01-10"


def test_api_error_response_is_raised():
    body = '{"result":"사용자 정보 검증에 실패하였습니다."}'.encode()
    with pytest.raises(LawApiError, match="검증"):
        _parse_xml(body)


def test_missing_oc_gives_clear_error():
    with pytest.raises(LawApiError, match="LAW_API_OC"):
        LawApiClient("")


def test_law_url_article():
    assert law_url("도시가스사업법 시행규칙", "별표 7").startswith("https://www.law.go.kr/%EB%B2%95%EB%A0%B9/")


def test_parse_kgs_list_page(fixture_bytes):
    rows = parse_list_page(fixture_bytes("kgs_list.html").decode("utf-8"), "01")
    assert len(rows) == 2
    r = rows[0]
    assert r["code"] == "AC116"
    assert r["pblc_cd"] == "AC116_260911"
    assert r["amended_date"] == "2026-09-11"
    assert r["file_nm"] == "kgscode_pdf/2026/AC116_260911.pdf"
    assert r["gas_category"] == "고압가스"
    assert "cyberJSESSIONID" not in r["view_url"]
    assert rows[1]["file_nm"] is None
    assert "  " not in rows[1]["name"]  # 줄바꿈 정리
