"""질문 로그 저장 (F-15, PRD §5.6).

- Supabase 설정이 있으면 query_logs 테이블에 저장 (스키마: docs/supabase_schema.sql)
- 없으면 data/logs/queries.jsonl 에 저장 (개발용. Streamlit Cloud에서는 재시작 시 사라짐)
- 저장 전 전화번호·주민등록번호·이메일·카드번호 패턴을 마스킹한다. IP는 저장하지 않는다.
- 저장은 백그라운드 스레드에서 하고, 실패해도 예외를 밖으로 던지지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from core.config import LOGS_DIR, get_supabase_config
from core.models import QueryLog

log = logging.getLogger(__name__)
TABLE = "query_logs"
LOCAL_PATH = LOGS_DIR / "queries.jsonl"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="querylog")
_file_lock = threading.Lock()
_client = None
_client_lock = threading.Lock()

# 순서 중요: 주민번호(13자리) → 카드번호(16자리) → 전화번호
# \b는 한글도 단어 문자로 보므로("010-1234-5678로") 숫자 앞뒤만 확인한다.
_ND = r"(?<!\d)"
_NA = r"(?!\d)"
_PII_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), "[이메일]"),
    (re.compile(_ND + r"\d{6}\s*-\s*[1-8]\d{6}" + _NA), "[주민등록번호]"),
    (re.compile(_ND + r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[1-8]\d{6}" + _NA), "[주민등록번호]"),
    (re.compile(_ND + r"\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}" + _NA), "[카드번호]"),
    (re.compile(_ND + r"01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}" + _NA), "[전화번호]"),
    (re.compile(_ND + r"0(?:2|[3-6][1-5]|70)[-.\s]?\d{3,4}[-.\s]?\d{4}" + _NA), "[전화번호]"),
]


def mask_pii(text: str) -> str:
    """개인정보 패턴을 마스킹한다."""
    for pattern, repl in _PII_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _supabase():
    global _client
    cfg = get_supabase_config()
    if not cfg:
        return None
    with _client_lock:
        if _client is None:
            from supabase import create_client

            _client = create_client(*cfg)
    return _client


def _write_local(record: dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with _file_lock, LOCAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _save(record: dict[str, Any]) -> bool:
    try:
        client = _supabase()
        if client is not None:
            client.table(TABLE).insert(record).execute()
        else:
            _write_local({"event": "query", **record})
        return True
    except Exception:  # 로그 실패가 답변을 막아서는 안 된다
        log.exception("질문 로그 저장 실패")
        return False


def prepare(log_entry: QueryLog) -> dict[str, Any]:
    record = log_entry.to_dict()
    record["question"] = mask_pii(record["question"])
    record["answer"] = mask_pii(record["answer"])
    return record


def save_log(log_entry: QueryLog, background: bool = True) -> bool:
    """로그를 저장한다. background=True면 즉시 True를 반환하고 스레드에서 저장한다."""
    try:
        record = prepare(log_entry)
    except Exception:
        log.exception("질문 로그 준비 실패")
        return False
    if background:
        _executor.submit(_save, record)
        return True
    return _save(record)


def _save_feedback(log_id: str, feedback: str) -> bool:
    try:
        client = _supabase()
        if client is not None:
            client.table(TABLE).update({"feedback": feedback}).eq("id", log_id).execute()
        else:
            _write_local({"event": "feedback", "id": log_id, "feedback": feedback})
        return True
    except Exception:
        log.exception("피드백 저장 실패")
        return False


def save_feedback(log_id: str, feedback: str, background: bool = True) -> bool:
    """기존 로그에 피드백(up/down/의견)을 기록한다."""
    feedback = mask_pii(feedback)
    if background:
        _executor.submit(_save_feedback, log_id, feedback)
        return True
    return _save_feedback(log_id, feedback)


def read_logs(limit: int = 200) -> list[dict[str, Any]]:
    """관리자 페이지용: 최근 로그 (최신순). 로컬 파일은 피드백 이벤트를 합쳐서 돌려준다."""
    client = _supabase()
    if client is not None:
        res = client.table(TABLE).select("*").order("timestamp", desc=True).limit(limit).execute()
        return list(res.data or [])
    if not LOCAL_PATH.exists():
        return []
    rows: dict[str, dict[str, Any]] = {}
    with _file_lock:
        lines = LOCAL_PATH.read_text(encoding="utf-8").splitlines()
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "feedback":
            if rec.get("id") in rows:
                rows[rec["id"]]["feedback"] = rec.get("feedback")
        else:
            rec.pop("event", None)
            rows[rec["id"]] = rec
    return sorted(rows.values(), key=lambda r: r.get("timestamp", ""), reverse=True)[:limit]


def storage_label() -> str:
    return "Supabase" if get_supabase_config() else f"로컬 파일 ({LOCAL_PATH.name}, 개발용)"
