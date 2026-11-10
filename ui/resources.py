"""프로세스 전체에서 공유하는 Streamlit 리소스.

질의응답·관리자 화면이 같은 객체를 쓰도록 한곳에 모은다. 관리자 화면은 색인만 다시 불러올 때
`get_retriever.clear()`만 호출해야 한다 — `st.cache_resource.clear()`는 사용량 카운터와 답변 캐시까지
비워 한도가 초기화된다 (REVIEW M6).
"""

from __future__ import annotations

from collections import OrderedDict

import streamlit as st

from core.config import CHUNKS_DIR, INDEX_DIR, get_setting, today_kst
from core.embeddings import get_embedder
from core.rate_limit import LoginThrottle, RateLimiter, pick_client_ip
from core.retriever import Retriever


@st.cache_resource(max_entries=1, show_spinner="검색 색인을 준비하는 중입니다... (처음 한 번, 몇 초 걸립니다)")
def get_retriever(data_version: str, day: str) -> Retriever:
    """data_version(재배포로 새 데이터)이나 날짜(KST)가 바뀌면 다시 만든다.

    날짜를 키에 넣는 이유: 시행일이 지난 조문의 '시행예정' 표시를 매일 다시 계산하기 위해 (REVIEW M4).
    max_entries=1이라 이전 날짜·버전 색인(약 300MB)은 메모리에서 내려간다.
    """
    return Retriever.from_directory(CHUNKS_DIR, INDEX_DIR, embedder=get_embedder())


def current_retriever(data_version: str) -> Retriever:
    return get_retriever(data_version, today_kst().isoformat())


@st.cache_resource
def get_limiter() -> RateLimiter:
    return RateLimiter()


@st.cache_resource
def get_login_throttle() -> LoginThrottle:
    return LoginThrottle()


@st.cache_resource
def get_answer_cache() -> OrderedDict:
    return OrderedDict()


def _headers() -> dict[str, str]:
    try:
        return dict(st.context.headers)
    except Exception:
        return {}


def client_ip() -> str | None:
    headers = {k.lower(): v for k, v in _headers().items()}
    try:
        direct = st.context.ip_address
    except Exception:
        direct = None
    return pick_client_ip(headers.get("x-forwarded-for"), direct, get_setting("TRUSTED_PROXY_HOPS"))


def connection_info() -> dict[str, object]:
    """관리자 화면용: 프록시 구성을 확인해 TRUSTED_PROXY_HOPS를 맞추는 데 쓴다. IP 값은 가린다."""
    headers = {k.lower(): v for k, v in _headers().items()}
    xff = headers.get("x-forwarded-for")
    parts = [p.strip() for p in xff.split(",")] if xff else []

    def mask(ip: str | None) -> str:
        if not ip:
            return "(없음)"
        if ":" in ip:
            return ip.split(":")[0] + ":…"
        return ".".join(ip.split(".")[:2] + ["x", "x"])  # '*'는 표에서 markdown 강조로 사라진다

    try:
        direct = st.context.ip_address
    except Exception:
        direct = None
    return {
        "X-Forwarded-For 항목 수": len(parts),
        "X-Forwarded-For (가림)": ", ".join(mask(p) for p in parts) or "(없음)",
        "직접 접속 IP (가림)": mask(direct),
        "TRUSTED_PROXY_HOPS": get_setting("TRUSTED_PROXY_HOPS"),
        "선택된 IP (가림)": mask(client_ip()),
    }
