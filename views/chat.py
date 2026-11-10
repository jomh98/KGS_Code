"""질의응답 화면.

질문 → 사용량 확인(F-16) → 근거 검색(F-02) → 답변 생성·스트리밍(F-03) → 인용 검증 → 출처 표시(F-04, F-05)
→ 질문 로그(F-15).
"""

from __future__ import annotations

from uuid import uuid4

import streamlit as st

from core.config import get_setting, load_meta
from core.generator import AnswerStream, will_call_llm
from core.logger import save_feedback, save_log
from core.models import Answer, QueryLog
from core.retriever import Retriever, SearchFilters, gas_hints
from ui.components import (
    DISCLAIMER,
    LOG_NOTICE,
    has_law_data,
    render_answer_actions,
    render_answer_body,
    render_header,
    render_notices,
    render_scope_notice,
    render_sources,
)
from ui.resources import client_ip, current_retriever, get_answer_cache, get_limiter
from ui.styles import ASSISTANT_AVATAR, GAS_ICONS, SOURCE_LABEL, USER_AVATAR

GAS_OPTIONS = ["고압가스", "LPG", "도시가스", "수소"]
DOC_OPTIONS = ["법률", "시행령", "시행규칙", "행정규칙", "KGS"]
EXAMPLES = [
    ("고압가스", "고압가스 저장탱크 재검사 항목은 무엇인가요?"),
    ("LPG", "LPG 용기 보관실의 설치 기준은?"),
    ("도시가스", "도시가스 배관을 지하에 매설할 때 매설 깊이 기준은?"),
    ("수소", "수소충전소의 압축설비와 저장설비 사이 거리 기준은?"),
]
ANSWER_CACHE_SIZE = 300


def cache_key(question: str, filters: SearchFilters, data_version: str) -> str:
    return f"{' '.join(question.split()).lower()}|{filters.key()}|{data_version}"


# ── 세션 ───────────────────────────────────────────────────


def init_session() -> None:
    st.session_state.setdefault("session_id", uuid4().hex)
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("pending_question", None)


def on_feedback(log_id: str, key: str) -> None:
    value = st.session_state.get(key)
    if value is not None:
        save_feedback(log_id, "up" if value == 1 else "down")
        st.toast("의견 감사합니다. 품질 개선에 반영하겠습니다.", icon=":material/thumb_up:")


def export_markdown(question: str, answer: Answer) -> str:
    lines = [f"# 질문\n\n{question}\n", f"# 답변\n\n{answer.text}\n", "# 근거\n"]
    for n, rc in enumerate(answer.sources, 1):
        if answer.cited and n not in answer.cited:
            continue
        lines.append(f"{n}. {rc.chunk.citation} — {rc.chunk.url}")
    lines.append(f"\n---\n{DISCLAIMER}")
    return "\n".join(lines)


def render_answer(answer: Answer, question: str, log_id: str | None) -> None:
    render_answer_body(answer)
    render_sources(answer, question_gas=gas_hints(question), stale_codes=set(load_meta().get("kgs_stale") or []))
    render_answer_actions(answer, export_markdown(question, answer), log_id, on_feedback)


# ── 사이드바 ───────────────────────────────────────────────


def render_sidebar(meta: dict, retriever: Retriever, remaining: int) -> SearchFilters:
    with st.sidebar:
        st.subheader(":material/filter_alt: 검색 범위")
        gas = st.multiselect(
            "가스 종류", GAS_OPTIONS, placeholder="전체", help="선택하면 '공통' 기준도 함께 검색합니다."
        )
        docs = st.multiselect(
            "문서 유형", DOC_OPTIONS, placeholder="전체", format_func=lambda s: SOURCE_LABEL.get(s, s)
        )

        st.subheader(":material/database: 데이터")
        st.caption(f"법령 기준일: {meta.get('law_updated_at') or '미수집'}")
        st.caption(f"KGS Code 기준일: {meta.get('kgs_updated_at') or '미수집'}")
        by_src = meta.get("chunk_count_by_source") or {}
        if by_src:
            st.caption(
                "색인: " + ", ".join(f"{SOURCE_LABEL.get(k, k)} {v:,}건" for k, v in by_src.items())
                + (" · 의미 검색 사용" if retriever.uses_vectors else "")
            )

        st.subheader(":material/hourglass_top: 이용 현황")
        limit = get_setting("MAX_QUESTIONS_PER_CLIENT_PER_DAY")
        st.progress(min(remaining / limit, 1.0) if limit else 0.0, text=f"오늘 남은 질문 {remaining} / {limit}회")
        if st.button("새 대화", icon=":material/add_comment:", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

        st.divider()
        st.caption(f":material/info: {LOG_NOTICE}")
        st.caption(f":material/gavel: {DISCLAIMER}")
    return SearchFilters(gas_categories=gas, source_types=docs)


# ── 질문 처리 ──────────────────────────────────────────────


def handle_question(question: str, filters: SearchFilters, retriever: Retriever, data_version: str) -> None:
    """질문 1건 처리.

    사용량: LLM을 부를 때만 호출 **전에** 1회를 예약(reserve)하고, 오류로 답을 못 만들면 환급(refund).
    캐시: 정상 완료된 답변(Answer.cacheable)만 저장한다 — 오류·거절·잘린 답은 저장하지 않는다 (REVIEW C2).
    """
    limiter = get_limiter()
    key = limiter.client_key(client_ip(), st.session_state.session_id)
    cache = get_answer_cache()
    ckey = cache_key(question, filters, data_version)
    cached: Answer | None = cache.get(ckey)

    reserved = False
    sources = []
    if cached is None:
        limit = limiter.check(key, question)
        if not limit.allowed:
            st.warning(limit.message, icon=":material/hourglass_empty:")
            return
        with st.spinner("관련 법령·KGS Code를 찾는 중..."):
            sources = retriever.search(question, filters, top_k=get_setting("TOP_K"))
        if will_call_llm(sources):
            res = limiter.reserve(key)  # 확인과 차감을 한 번에 → 동시 요청으로 한도를 넘지 못함
            if not res.allowed:
                st.warning(res.message, icon=":material/hourglass_empty:")
                return
            reserved = True

    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(question)

    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        if cached is not None:
            answer = cached
        else:
            stream = AnswerStream(question, sources)
            placeholder = st.empty()
            try:
                with placeholder.container(), st.spinner("근거를 바탕으로 답변을 작성하는 중..."):
                    st.write_stream(stream)
            finally:
                placeholder.empty()
                answer = stream.answer
                if reserved and (answer is None or answer.error or not answer.llm_called):
                    limiter.refund(key)  # 오류로 답을 못 만든 경우 사용량을 돌려준다
            if answer.cacheable:
                cache[ckey] = answer
                while len(cache) > ANSWER_CACHE_SIZE:
                    cache.popitem(last=False)

        log_entry = QueryLog(
            session_id=st.session_state.session_id,
            question=question,
            answer=answer.text,
            cited_sources=[answer.sources[n - 1].chunk.chunk_id for n in answer.cited]
            or [s.chunk.chunk_id for s in answer.sources],
            filters={"gas": filters.gas_categories, "docs": filters.source_types},
            latency_ms=0 if cached is not None else answer.latency_ms,
            model="cache" if cached is not None else answer.model,
            data_version=data_version,
            insufficient_evidence=answer.insufficient_evidence,
            error=answer.error,
            removed_citations=answer.removed_citations,
        )
        save_log(log_entry)
        render_answer(answer, question, log_entry.id)

    st.session_state.messages.append(
        {"role": "assistant", "answer": answer, "question": question, "log_id": log_entry.id}
    )


def render_welcome(enabled: bool) -> None:
    st.markdown(
        "가스 시설의 설치·검사·안전관리 기준을 물어보세요. 현재 시행 중인 기준에서 근거를 찾아 "
        "**조항·코드 번호와 원문 링크**를 함께 보여 드립니다."
    )
    st.markdown("**이렇게 물어보세요**")
    for i, (label, q) in enumerate(EXAMPLES):
        if i % 2 == 0:
            cols = st.columns(2)  # 행 단위로 만들어 모바일(세로 쌓임)에서도 순서 유지
        with cols[i % 2]:
            if st.button(
                q,
                icon=GAS_ICONS.get(label),
                help=f"{label} 예시 질문",
                use_container_width=True,
                wrap=True,
                disabled=not enabled,
                key=f"ex_{i}",
            ):
                st.session_state.pending_question = q
                st.rerun()
    render_notices()


def main() -> None:
    init_session()
    meta = load_meta()
    data_version = str(meta.get("index_built_at") or "none")
    render_header(meta)  # 색인을 처음 만드는 몇 초 동안에도 빈 화면이 아니도록 먼저 그린다
    retriever = current_retriever(data_version)
    limiter = get_limiter()
    remaining = limiter.remaining(limiter.client_key(client_ip(), st.session_state.session_id))
    filters = render_sidebar(meta, retriever, remaining)

    if not retriever.chunks:
        st.error(
            "아직 검색할 데이터가 준비되지 않았습니다. 잠시 후 다시 이용해 주세요.",
            icon=":material/database_off:",
        )
    elif not has_law_data(meta):
        render_scope_notice()  # 법령 미수집 상태를 사용자에게 알린다 (REVIEW C3)

    if not st.session_state.messages:
        render_welcome(enabled=bool(retriever.chunks) and remaining > 0)

    for msg in st.session_state.messages:
        if msg["role"] == "user":
            with st.chat_message("user", avatar=USER_AVATAR):
                st.markdown(msg["content"])
        else:
            with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
                render_answer(msg["answer"], msg["question"], msg["log_id"])

    out_of_quota = remaining <= 0
    if out_of_quota:
        st.warning(
            f"오늘 질문 가능 횟수({get_setting('MAX_QUESTIONS_PER_CLIENT_PER_DAY')}회)를 모두 사용했습니다. "
            "내일 다시 이용해 주세요. 이미 받은 답변과 근거는 계속 볼 수 있습니다.",
            icon=":material/hourglass_empty:",
        )
    typed = st.chat_input(
        "오늘 질문 횟수를 모두 사용했습니다" if out_of_quota else "가스 안전 기준을 질문하세요",
        max_chars=get_setting("MAX_QUESTION_LENGTH"),
        disabled=out_of_quota,
    )
    question = typed or st.session_state.pending_question
    st.session_state.pending_question = None
    if question:
        handle_question(question, filters, retriever, data_version)


main()
