from datetime import date

from ingest.chunker import MAX_CHUNK_CHARS, chunk_kgs, chunk_law, find_kgs_sections, split_long
from ingest.law_api import parse_law_service

CODE = {
    "code": "TS001",
    "year": "2026",
    "name": "(샘플) 테스트 기준",
    "pblc_cd": "TS001_260101",
    "view_url": "https://cyber.kgs.or.kr/kgscode.codeSearch.view.ex.do?pblcCd=TS001_260101",
    "amended_date": "2026-01-01",
    "gas_category": "도시가스",
}


def test_split_long_respects_limit_and_keeps_text():
    text = "\n".join(f"{i}번째 줄 " + "가" * 90 for i in range(60))
    parts = split_long(text)
    assert len(parts) > 1
    assert all(len(p) <= MAX_CHUNK_CHARS for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_split_long_cuts_single_overlong_line():
    parts = split_long("가" * (MAX_CHUNK_CHARS * 2 + 10))
    assert [len(p) for p in parts] == [MAX_CHUNK_CHARS, MAX_CHUNK_CHARS, 10]


def test_kgs_sections_ignore_numbers_inside_tables():
    pages = [
        (1, "1. 일반사항\n1.1 적용범위\n이 기준은 샘플에 적용한다.\n1.2 용어정의\n용어 설명"),
        # 표 속 숫자 줄: "3. 크롬함유량…", "3.5 이상" 은 제목이 아니다
        (2, "2. 시설기준\n2.1 배치\n재료 표\n3. 크롬함유량이 많은 재료\n3.5 이상\n2.2 배관\n2.2.1 배관 지하매설\n매설 깊이는 1m 이상"),
        (3, "3. 기술기준\n3.1 운영\n운영 기준"),
    ]
    nums = [s["num"] for s in find_kgs_sections(pages)]
    assert nums == ["1", "1.1", "1.2", "2", "2.1", "2.2", "2.2.1", "3", "3.1"]
    sec = {s["num"]: s for s in find_kgs_sections(pages)}
    assert "3.5 이상" in sec["2.1"]["text"]  # 본문에 남는다
    assert sec["2.2.1"]["chapter"].endswith("2.2 배관")  # 상위 절 경로


def test_kgs_chapter_without_children_is_not_heading():
    pages = [(1, "1. 일반사항\n1.1 적용범위\n" + "본문 " * 40 + "\n2. 제조시설기준(해당 없음)\n3. 검사기준\n3.1 검사\n검사 본문 " * 1)]
    nums = [s["num"] for s in find_kgs_sections(pages)]
    assert "2" not in nums
    assert "3" in nums and "3.1" in nums


def test_chunk_kgs_metadata():
    pages = [(1, "1. 일반사항\n1.1 적용범위\n" + "이 기준은 도시가스 배관에 적용한다. " * 10)]
    chunks = chunk_kgs(CODE, pages)
    assert chunks
    c = chunks[-1]
    assert c.source_type == "KGS"
    assert c.doc_name == "KGS TS001"
    assert c.amended_date == "2026-01-01"
    assert c.url == CODE["view_url"]
    assert c.page == 1
    assert c.text.startswith("[KGS TS001 2026 (샘플) 테스트 기준]")
    assert len({ch.chunk_id for ch in chunks}) == len(chunks)


def test_chunk_law_from_parsed_record(fixture_bytes):
    rec = parse_law_service(fixture_bytes("law_service.xml"), today=date(2026, 1, 1))
    rec.update(priority="A", gas_category="도시가스")
    chunks = chunk_law(rec)
    labels = [c.article_or_item for c in chunks]
    assert "제17조의5(가스공급시설의 시설기준 등)" in labels
    assert any(l.startswith("[별표 7]") for l in labels)
    art = next(c for c in chunks if c.article_or_item.startswith("제17조의5"))
    assert "배관의 매설 기준" in art.text  # 항·호 내용까지 합쳐짐
    assert art.url.endswith("%EC%A0%9C17%EC%A1%B0%EC%9D%985")  # .../제17조의5
    future = next(c for c in chunks if c.article_or_item.startswith("제30조"))
    assert future.status == "시행예정"
    assert len({c.chunk_id for c in chunks}) == len(chunks)
