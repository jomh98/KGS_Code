"""화면 보조 함수 (Streamlit 실행 없이 확인 가능한 부분)."""

from core.models import Chunk, RetrievedChunk
from ui.components import doc_title, escape_md, split_summary
from ui.styles import source_badge


def test_split_summary_standard_format():
    text = "**답변:** 지면에서 1m 이상 깊이에 매설합니다. [근거 1]\n\n**세부 내용:**\n- 도로: 1.2m 이상 [근거 2]"
    summary, rest = split_summary(text)
    assert summary == "지면에서 1m 이상 깊이에 매설합니다. [근거 1]"
    assert rest.startswith("**세부 내용:**")


def test_split_summary_without_format_returns_all():
    text = "형식 없이 쓴 답변입니다."
    assert split_summary(text) == (None, text)


def test_split_summary_summary_only():
    summary, rest = split_summary("**답변:** 결론만 있습니다.")
    assert summary == "결론만 있습니다."
    assert rest == ""


def test_escape_md_blocks_links_and_math():
    escaped = escape_md("[별표 7](javascript:alert) $x$ **굵게**")
    assert "\\[" in escaped and "\\$" in escaped and "\\*" in escaped


def test_doc_title_from_kgs_chunk():
    ch = Chunk(
        chunk_id="k", source_type="KGS", doc_name="KGS FP651", article_or_item="2.5.10", url="u",
        text="[KGS FP651 2026 고정식 압축도시가스자동차 충전의 시설·기술·검사 기준] 2 시설 기준\n본문",
    )
    assert doc_title(RetrievedChunk(ch, 1.0)) == "고정식 압축도시가스자동차 충전의 시설·기술·검사 기준"


def test_doc_title_none_for_law():
    ch = Chunk(chunk_id="l", source_type="법률", doc_name="법", article_or_item="제1조", url="u", text="[법] 제1조")
    assert doc_title(RetrievedChunk(ch, 1.0)) is None


def test_source_badge_colors_avoid_warning_colors():
    for stype in ("법률", "시행령", "시행규칙", "행정규칙", "KGS"):
        badge = source_badge(stype)
        assert ":orange-" not in badge and ":red-" not in badge
    assert source_badge("KGS") == ":primary-badge[KGS Code]"
