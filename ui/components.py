"""재사용 화면 조각: 헤더, 근거 카드, 답변 카드, 안내 문구."""

from __future__ import annotations

import re
from collections.abc import Callable

import streamlit as st

from core.models import Answer, RetrievedChunk
from core.retriever import chunk_gas_tags
from ui.styles import APP_ICON, APP_TITLE, source_badge

DISCLAIMER = (
    "본 답변은 참고용이며 법적 해석의 효력이 없습니다. 정확한 적용은 원문 및 "
    "관할 기관(한국가스안전공사 등)에 확인하세요."
)
LOG_NOTICE = "질문 내용은 서비스 품질 개선을 위해 저장됩니다. 이름·연락처 등 개인정보는 입력하지 마세요."

_MD_SPECIAL = re.compile(r"([\\`*_\[\]$<>#|~])")


def escape_md(text: str) -> str:
    """법령·코드 제목을 markdown에 넣을 때 링크·강조·수식으로 해석되지 않게 한다."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def date_badge(label: str, value: str | None, color: str) -> str:
    if value:
        return f":{color}-badge[{label} {value}]"
    return f":gray-badge[{label} 미수집]"


def render_header(meta: dict) -> None:
    st.title(f"{APP_ICON} {APP_TITLE}")
    st.markdown(
        "현행 가스 안전 법령과 KGS Code를 근거로 답합니다.  \n"
        + date_badge("법령 기준일", meta.get("law_updated_at"), "blue")
        + " "
        + date_badge("KGS Code 기준일", meta.get("kgs_updated_at"), "green")
    )


LAW_SOURCE_TYPES = ("법률", "시행령", "시행규칙", "행정규칙")


def has_law_data(meta: dict) -> bool:
    by_src = meta.get("chunk_count_by_source") or {}
    return any(by_src.get(t) for t in LAW_SOURCE_TYPES)


def render_scope_notice() -> None:
    """법령이 아직 색인되지 않았을 때: 무엇을 검색하는지 정확히 알린다 (REVIEW C3)."""
    st.info(
        "현재는 **KGS Code(가스기술기준)만** 검색합니다. 법률·시행령·시행규칙은 준비 중이라, "
        "허가·신고·안전관리자 선임·과태료처럼 법령에서 정하는 사항은 "
        "[국가법령정보센터](https://www.law.go.kr)에서 원문을 확인해 주세요.",
        icon=":material/info:",
    )


def render_notices() -> None:
    """첫 화면 안내: 눈에 띄되 거슬리지 않게 한 상자에 작게."""
    with st.container(border=True):
        st.caption(f":material/info: {LOG_NOTICE}")
        st.caption(f":material/gavel: {DISCLAIMER}")


# ── 근거 ───────────────────────────────────────────────────


_KGS_TITLE = re.compile(r"^\[KGS \S+ \d{4} (?P<name>[^\]]+)\]")
_LONG_TEXT = 700  # 이보다 긴 원문은 스크롤 상자에 넣는다


def doc_title(rc: RetrievedChunk) -> str | None:
    """KGS 청크 첫 줄 '[KGS FP651 2026 코드명]'에서 코드명을 꺼낸다."""
    m = _KGS_TITLE.match(rc.chunk.text) if rc.chunk.source_type == "KGS" else None
    return m.group("name").strip() if m else None


def render_source_card(
    n: int, rc: RetrievedChunk, question_gas: set[str] | None = None, stale_codes: set[str] | None = None
) -> None:
    ch = rc.chunk
    with st.container(border=True):
        badges = source_badge(ch.source_type)
        if ch.status == "시행예정":
            when = f" {ch.effective_date}" if ch.effective_date else ""
            badges += f" :orange-badge[:material/schedule: 시행예정{when}]"
        # 질문은 수소인데 근거는 도시가스처럼 분야가 다른 경우 (REVIEW M10)
        tags = chunk_gas_tags(ch)
        if question_gas and "공통" not in tags and not (tags & question_gas):
            badges += f" :orange-badge[:material/swap_horiz: 다른 분야({ch.gas_category}) 기준]"
        code = ch.doc_name.removeprefix("KGS ").strip()
        if stale_codes and ch.source_type == "KGS" and code in stale_codes:
            badges += " :orange-badge[:material/sync_problem: 최신 개정판 미반영 — 원문 확인]"
        st.markdown(f"{badges} **근거 {n}** · {escape_md(ch.doc_name)}")
        title = doc_title(rc)
        if title:
            st.caption(escape_md(title))
        st.markdown(escape_md(ch.article_or_item))

        info = []
        if ch.source_type == "KGS":
            if ch.amended_date:
                info.append(f"개정 {ch.amended_date}")
            if ch.page:
                info.append(f"{ch.page}쪽")
        elif ch.effective_date:
            info.append(f"시행 {ch.effective_date}")
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            if info:
                st.caption(" · ".join(info))
            st.space("stretch")
            st.link_button("원문", ch.url, icon=":material/open_in_new:", type="tertiary")
        with st.expander("원문 내용 보기"):
            if len(ch.text) > _LONG_TEXT:
                with st.container(height=320, border=False):
                    st.text(ch.text)
            else:
                st.text(ch.text)


def render_sources(
    answer: Answer, question_gas: set[str] | None = None, stale_codes: set[str] | None = None
) -> None:
    if not answer.sources:
        return
    cited = answer.cited or list(range(1, len(answer.sources) + 1))
    title = "답변 근거" if answer.cited else "검색된 근거"
    st.markdown(f"**{title}** :gray-badge[{len(cited)}건]")
    for n in cited:
        render_source_card(n, answer.sources[n - 1], question_gas, stale_codes)
    others = [n for n in range(1, len(answer.sources) + 1) if n not in cited]
    if others:
        with st.expander(f"그 밖에 검색된 근거 {len(others)}건"):
            for n in others:
                ch = answer.sources[n - 1].chunk
                st.markdown(f"- {source_badge(ch.source_type)} 근거 {n} · [{escape_md(ch.citation)}]({ch.url})")


# ── 답변 ───────────────────────────────────────────────────

_SUMMARY = re.compile(r"^\s*\*\*답변:?\*\*:?\s*(?P<summary>.+?)(?:\n\s*\n|\Z)(?P<rest>.*)\Z", re.S)


def split_summary(text: str) -> tuple[str | None, str]:
    """'**답변:** 결론…' 형식이면 (결론, 나머지)로 나눈다. 형식이 다르면 (None, 전체)."""
    m = _SUMMARY.match(text)
    if not m:
        return None, text
    return m.group("summary").strip(), m.group("rest").strip()


def render_answer_body(answer: Answer) -> None:
    text = answer.text
    if answer.insufficient_evidence:
        with st.container(border=True):
            st.markdown(":orange-badge[:material/search_off: 근거 없음]")
            st.markdown(text)
            st.caption("도움말: 가스 종류(LPG·도시가스 등), 시설(저장탱크·배관 등), 항목(거리·두께 등)을 넣어 물어보면 찾기 쉽습니다.")
        return
    if answer.error:
        st.error(text, icon=":material/error:")
        return
    if answer.model == "none":
        st.info(text, icon=":material/manage_search:")
        return
    summary, rest = split_summary(text)
    if summary:
        with st.container(border=True):
            st.markdown(":blue-badge[:material/check_circle: 답변 요약]")
            st.markdown(f"**{summary}**")
        if rest:
            st.markdown(rest)
    else:
        st.markdown(text)


def render_answer_actions(
    answer: Answer,
    export_md: str,
    log_id: str | None,
    on_feedback: Callable[[str, str], None] | None,
) -> None:
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        if log_id and on_feedback:
            key = f"fb_{log_id}"
            st.feedback("thumbs", key=key, on_change=on_feedback, args=(log_id, key))
        st.download_button(
            "내보내기",
            export_md,
            file_name="가스안전_답변.md",
            mime="text/markdown",
            icon=":material/download:",
            type="tertiary",
            key=f"dl_{log_id}",
        )
    st.caption(f":material/gavel: {DISCLAIMER}")
