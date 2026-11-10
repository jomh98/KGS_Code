"""설정·비밀정보 로딩.

조회 순서: st.secrets → 환경변수. 값이 없으면 None을 반환하고 앱은 계속 실행된다
(키가 없을 때의 안내는 app.py가 missing_keys()로 표시).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CHUNKS_DIR = DATA_DIR / "chunks"
INDEX_DIR = DATA_DIR / "index"
RAW_DIR = DATA_DIR / "raw"
META_PATH = DATA_DIR / "meta.json"
TARGET_LAWS_PATH = ROOT / "config" / "target_laws.yaml"

# (secrets 섹션, 키 이름, 환경변수 이름, 화면에 보여줄 설명)
REQUIRED_KEYS: list[tuple[str | None, str, str, str]] = [
    (None, "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "Claude API 키 (답변 생성)"),
    (None, "LAW_API_OC", "LAW_API_OC", "국가법령정보 Open API OC (데이터 수집)"),
    (None, "EMBEDDING_API_KEY", "EMBEDDING_API_KEY", "임베딩 API 키 (없으면 키워드 검색만 사용)"),
    (None, "ADMIN_PASSWORD", "ADMIN_PASSWORD", "관리자 페이지 비밀번호"),
    ("supabase", "url", "SUPABASE_URL", "Supabase URL (질문 로그 저장)"),
    ("supabase", "key", "SUPABASE_KEY", "Supabase 키 (질문 로그 저장)"),
]

LOGS_DIR = DATA_DIR / "logs"  # Supabase 미설정 시 개발용 로컬 로그 (git 제외)

# 모델·운영 기본값 (PRD §13.2 미결 사항이므로 secrets/환경변수로 덮어쓸 수 있게 둔다)
DEFAULTS: dict[str, Any] = {
    "LLM_MODEL": "claude-opus-5-5",
    "LLM_EFFORT": "low",  # low | medium | high — Q&A는 low로 시작, 품질 평가 후 조정
    "EMBEDDING_MODEL": "voyage-3.5",
    # 한 사용자(브라우저/IP)당 하루 질문 수 — 새로고침해도 초기화되지 않음
    "MAX_QUESTIONS_PER_CLIENT_PER_DAY": 20,
    # 앱 전체 하루 질문 수 (비용 상한)
    "MAX_QUESTIONS_PER_DAY": 500,
    "MAX_QUESTION_LENGTH": 500,
    # 사고(thinking) 토큰도 포함되므로 너무 낮게 잡으면 답변이 잘린다
    "MAX_ANSWER_TOKENS": 8000,
    # 질문 속 가스 분야 용어가 최상위 근거에 들어 있는 비율이 이 값 미만이면 LLM을 호출하지 않는다 (F-06)
    # (실데이터 19,002청크, tests/gate_questions.yaml 32문항: 관련 질문 ≥0.57, 무관한 질문 0.00)
    # 데이터·토크나이저를 바꾸면 `python -m scripts.eval_gate`로 다시 확인할 것
    "MIN_RETRIEVAL_SCORE": 0.2,
    "TOP_K": 6,
    # X-Forwarded-For에서 클라이언트 IP를 고를 위치(오른쪽에서 몇 번째). 앞쪽 값은 클라이언트가 위조할 수
    # 있으므로 신뢰하는 프록시가 붙인 값만 쓴다. 배포 후 관리자 화면의 '접속 정보'로 확인해 맞출 것.
    "TRUSTED_PROXY_HOPS": 1,
    # 관리자 화면의 '수집·재색인 실행' 버튼. Streamlit Cloud에서는 결과가 휘발되고 자원을 많이 쓰므로 기본 꺼짐
    "ALLOW_ADMIN_REINDEX": 0,
    # 관리자 로그인: 이 횟수만큼 틀리면 잠깐 잠근다 (IP·세션별)
    "ADMIN_MAX_FAILS": 5,
    "ADMIN_LOCK_SECONDS": 300,
}

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst() -> date:
    """한국 날짜. 서버(Streamlit Cloud·GitHub Actions)는 UTC라서 date.today()를 쓰면 9시간 어긋난다."""
    return now_kst().date()


def _from_streamlit(section: str | None, key: str) -> Any:
    try:
        import streamlit as st

        secrets = st.secrets
        if section:
            return secrets.get(section, {}).get(key)
        return secrets.get(key)
    except Exception:  # secrets.toml이 없거나 Streamlit 밖에서 실행된 경우
        return None


def get_secret(key: str, section: str | None = None, env: str | None = None) -> str | None:
    """st.secrets → 환경변수 순으로 값을 찾는다. 빈 문자열은 없는 것으로 본다."""
    value = _from_streamlit(section, key)
    if not value:
        value = os.environ.get(env or (f"{section}_{key}".upper() if section else key))
    return value or None


def get_setting(name: str) -> Any:
    """운영 설정값. secrets/환경변수에 있으면 그 값을, 없으면 DEFAULTS를 쓴다."""
    default = DEFAULTS[name]
    raw = get_secret(name)
    if raw is None:
        return default
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


def get_supabase_config() -> tuple[str, str] | None:
    url = get_secret("url", section="supabase", env="SUPABASE_URL")
    key = get_secret("key", section="supabase", env="SUPABASE_KEY")
    return (url, key) if url and key else None


def missing_keys() -> list[str]:
    """설정되지 않은 키의 설명 목록."""
    return [
        desc
        for section, key, env, desc in REQUIRED_KEYS
        if get_secret(key, section=section, env=env) is None
    ]


def load_meta() -> dict[str, Any]:
    """data/meta.json (데이터 기준일·청크 수). 없거나 깨졌으면 빈 dict."""
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
