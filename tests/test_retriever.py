from core.models import Chunk
from core.retriever import Retriever, SearchFilters, query_tokens, tokenize


def test_tokenize_korean_bigrams_and_codes():
    toks = tokenize("LPG 용기보관실 KGS FS551")
    assert "용기" in toks and "보관" in toks and "관실" in toks
    assert "lpg" in toks and "fs551" in toks


def test_spacing_variants_match():
    # "용기 보관실"로 물어도 "용기보관실"과 맞아야 한다
    assert "기보" in tokenize("용기 보관실")
    assert "기보" in tokenize("용기보관실")


def test_query_stopwords_removed():
    assert "려줘" not in query_tokens("배관 기준 알려줘")


def test_search_finds_relevant(sample_chunks):
    r = Retriever(sample_chunks)
    res = r.search("도시가스 배관 매설 깊이 기준은?", top_k=3)
    assert res[0].chunk.chunk_id == "sample-kgs-1"
    assert res[0].coverage > 0.5


def test_unrelated_question_has_low_coverage(sample_chunks):
    r = Retriever(sample_chunks)
    res = r.search("오늘 점심 메뉴 추천해줘", top_k=3)
    assert not res or max(x.coverage for x in res) < 0.32


def test_gas_filter_keeps_common(sample_chunks):
    r = Retriever(sample_chunks)
    res = r.search("배관 매설", SearchFilters(gas_categories=["도시가스"]), top_k=10)
    cats = {x.chunk.gas_category for x in res}
    assert cats <= {"도시가스", "공통"}
    assert "공통" in cats


def test_source_type_filter(sample_chunks):
    r = Retriever(sample_chunks)
    res = r.search("배관 매설", SearchFilters(source_types=["KGS"]), top_k=10)
    assert res and all(x.chunk.source_type == "KGS" for x in res)


def test_priority_c_ranks_below_equal_a():
    base = dict(source_type="법률", article_or_item="제1조", url="u", gas_category="공통")
    chunks = [
        Chunk(chunk_id="c", doc_name="법C", text="배관 매설 깊이", priority="C", **base),
        Chunk(chunk_id="a", doc_name="법A", text="배관 매설 깊이", priority="A", **base),
    ]
    res = Retriever(chunks).search("배관 매설 깊이")
    assert [x.chunk.chunk_id for x in res] == ["a", "c"]


def test_max_chunks_per_doc():
    chunks = [
        Chunk(chunk_id=f"x{i}", source_type="KGS", doc_name="KGS ONE", article_or_item=str(i), text="배관 매설", url="u")
        for i in range(6)
    ] + [Chunk(chunk_id="y", source_type="KGS", doc_name="KGS TWO", article_or_item="1", text="배관 매설", url="u")]
    res = Retriever(chunks).search("배관 매설", top_k=10)
    assert sum(1 for x in res if x.chunk.doc_name == "KGS ONE") == 3
    assert any(x.chunk.doc_name == "KGS TWO" for x in res)


def test_future_status_is_kept_and_labeled(sample_chunks):
    r = Retriever(sample_chunks)
    res = r.search("탐지형 보호포", top_k=3)
    future = [x for x in res if x.chunk.status == "시행예정"]
    assert future and "[시행예정]" in future[0].chunk.citation


def test_empty_index():
    assert Retriever([]).search("배관") == []
