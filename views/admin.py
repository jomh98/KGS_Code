"""관리자 화면 (F-13). ADMIN_PASSWORD로 보호.

- 질문 로그 / 근거 없음 응답 / 피드백
- 데이터 상태 (기준일, 청크 수, 스캔본이라 색인하지 못한 KGS Code, 설정되지 않은 키)
- 색인 다시 불러오기, 수집·재색인 실행
"""

from __future__ import annotations

import hashlib
import hmac
import io
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import streamlit as st

from core.config import get_secret, get_setting, load_meta, missing_keys
from core.logger import read_logs, storage_label
from ui.resources import client_ip, connection_info, current_retriever, get_login_throttle, get_retriever
from ui.styles import SOURCE_LABEL

KST = timezone(timedelta(hours=9))


def kst(ts: str | None) -> str:
    """'2026-11-10T06:41:40+00:00' → '2026-11-10 15:41' (한국 시간)."""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _throttle_keys() -> list[str]:
    """시도 제한 키: 접속 IP와 세션 둘 다 센다 (새 세션을 열어도, IP를 바꿔도 혼자서는 못 빠져나가게)."""
    st.session_state.setdefault("admin_session_id", uuid4().hex)
    salt = get_secret("ADMIN_PASSWORD") or ""
    keys = [f"s:{st.session_state.admin_session_id}"]
    ip = client_ip()
    if ip:
        keys.append("ip:" + hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:24])
    return keys


def login_gate() -> bool:
    admin_password = get_secret("ADMIN_PASSWORD")
    if not admin_password:
        st.error("ADMIN_PASSWORD가 설정되지 않아 관리자 화면을 사용할 수 없습니다.", icon=":material/lock:")
        return False
    if st.session_state.get("is_admin"):
        return True

    throttle = get_login_throttle()
    keys = _throttle_keys()
    locked = max(throttle.locked_for(k) for k in keys)
    if locked:
        st.error(
            f"비밀번호를 여러 번 틀려 잠시 잠겼습니다. {locked}초 후에 다시 시도하세요.", icon=":material/lock_clock:"
        )
        return False

    with st.form("login", border=True):
        st.markdown("관리자 비밀번호를 입력하세요.")
        entered = st.text_input("비밀번호", type="password", label_visibility="collapsed")
        submitted = st.form_submit_button("로그인", type="primary", icon=":material/login:")
    if submitted:
        if entered and hmac.compare_digest(entered.encode(), admin_password.encode()):
            for k in keys:
                throttle.success(k)
            st.session_state.is_admin = True
            st.rerun()
        lock = max(throttle.failure(k) for k in keys)
        time.sleep(1.0)  # 자동 대입 속도를 늦춘다 (REVIEW M9)
        if lock:
            st.error(f"비밀번호를 여러 번 틀려 {lock}초 동안 잠겼습니다.", icon=":material/lock_clock:")
        else:
            st.error("비밀번호가 올바르지 않습니다.", icon=":material/error:")
    return False


def render_logs(logs: list[dict], log_error: str | None) -> None:
    st.caption(f"저장소: {storage_label()}")
    if log_error:
        st.error(f"로그를 읽지 못했습니다: {log_error}", icon=":material/error:")
        return
    if not logs:
        st.info("아직 저장된 질문이 없습니다.", icon=":material/inbox:")
        return
    with st.container(horizontal=True):
        st.metric("질문 수 (최근 500)", len(logs), border=True)
        st.metric("근거 없음", sum(1 for r in logs if r.get("insufficient_evidence")), border=True)
        st.metric("👍", sum(1 for r in logs if r.get("feedback") == "up"), border=True)
        st.metric("👎", sum(1 for r in logs if r.get("feedback") == "down"), border=True)
    st.dataframe(
        [
            {
                "시각(KST)": kst(r.get("timestamp")),
                "질문": r.get("question"),
                "근거없음": bool(r.get("insufficient_evidence")),
                "피드백": {"up": "👍", "down": "👎"}.get(r.get("feedback"), ""),
                "모델": r.get("model"),
                "응답(ms)": r.get("latency_ms"),
            }
            for r in logs
        ],
        use_container_width=True,
        hide_index=True,
        column_config={"질문": st.column_config.TextColumn(width="large")},
    )


def render_insufficient(logs: list[dict]) -> None:
    st.caption("근거를 찾지 못한 질문입니다. 수집 범위를 넓히거나 테스트 질문셋에 추가할 후보입니다.")
    rows = [r for r in logs if r.get("insufficient_evidence")]
    if not rows:
        st.info("근거 없음 응답이 없습니다.", icon=":material/check_circle:")
        return
    st.dataframe(
        [{"시각(KST)": kst(r.get("timestamp")), "질문": r.get("question")} for r in rows],
        use_container_width=True,
        hide_index=True,
    )


def render_data() -> None:
    meta = load_meta()
    with st.container(horizontal=True):
        st.metric("법령 기준일", meta.get("law_updated_at") or "미수집", border=True)
        st.metric("KGS Code 기준일", meta.get("kgs_updated_at") or "미수집", border=True)
        st.metric("청크 수", f"{meta.get('chunk_count', 0):,}", border=True)
    by_src = meta.get("chunk_count_by_source") or {}
    if by_src:
        st.caption("유형별: " + ", ".join(f"{SOURCE_LABEL.get(k, k)} {v:,}" for k, v in by_src.items()))
    if meta.get("kgs_no_text"):
        st.warning(
            "본문이 스캔 이미지라 색인하지 못한 KGS Code: " + ", ".join(meta["kgs_no_text"]),
            icon=":material/image_not_supported:",
        )
    if meta.get("kgs_stale"):
        st.warning(
            "새 개정판 반영에 실패해 이전 판을 쓰고 있는 KGS Code(화면에 '최신 개정판 미반영' 표시): "
            + ", ".join(meta["kgs_stale"]),
            icon=":material/sync_problem:",
        )
    if meta.get("kgs_failed"):
        with st.expander(f"지난 수집에서 실패한 문서 {len(meta['kgs_failed'])}건", icon=":material/error:"):
            for f in meta["kgs_failed"]:
                st.markdown(f"- {f}")
    retriever = current_retriever(str(meta.get("index_built_at") or "none"))
    if retriever.vector_error:
        st.warning(f"의미 검색 실패 — 키워드 검색만 사용 중: {retriever.vector_error}", icon=":material/warning:")
    missing = missing_keys()
    if missing:
        with st.expander(f"설정되지 않은 키 {len(missing)}개", icon=":material/key_off:"):
            for desc in missing:
                st.markdown(f"- {desc}")
            st.caption("`.streamlit/secrets.toml` 또는 Streamlit Cloud의 Secrets에 설정하세요.")

    st.divider()
    st.subheader("접속 정보 (사용량 제한 설정 확인용)")
    st.caption(
        "사용량 제한은 X-Forwarded-For의 오른쪽에서 TRUSTED_PROXY_HOPS번째 IP로 사용자를 구분합니다. "
        "배포 환경에서 '선택된 IP'가 매 접속마다 달라지거나 모두 같은 값이면 TRUSTED_PROXY_HOPS를 조정하세요."
    )
    st.table([{"항목": k, "값": str(v)} for k, v in connection_info().items()])

    st.divider()
    st.subheader("색인 다시 불러오기")
    st.caption(
        "data/ 폴더가 바뀐 뒤(재배포·수동 재색인) 앱이 새 데이터를 쓰도록 검색 색인만 다시 만듭니다. "
        "사용량 카운터와 답변 캐시는 그대로 둡니다."
    )
    if st.button("검색 색인 다시 불러오기", icon=":material/refresh:"):
        get_retriever.clear()  # st.cache_resource.clear()는 사용량 한도까지 초기화하므로 쓰지 않는다 (REVIEW M6)
        st.success("다음 질문부터 새 색인을 사용합니다.", icon=":material/check:")

    st.divider()
    st.subheader("수집·재색인 실행")
    if not get_setting("ALLOW_ADMIN_REINDEX"):
        st.info(
            "이 화면에서의 수집은 꺼져 있습니다 (ALLOW_ADMIN_REINDEX=0). Streamlit Cloud에서는 PDF 약 800MB를 "
            "내려받아 자원을 많이 쓰고, 결과도 재시작 시 사라집니다. 데이터 갱신은 GitHub Actions(Update data) "
            "또는 로컬에서 `python -m ingest.build_index` 실행 후 커밋하세요.",
            icon=":material/block:",
        )
        return
    st.warning(
        "여기서 만든 데이터는 Streamlit Cloud에서 재시작 시 사라집니다. "
        "법령 수집은 open.law.go.kr에 등록된 IP에서만 동작합니다.",
        icon=":material/warning:",
    )
    sources = st.multiselect("대상", ["kgs", "law"], default=["kgs"], format_func={"kgs": "KGS Code", "law": "법령"}.get)
    if st.button("실행", type="primary", icon=":material/play_arrow:", disabled=not sources):
        from ingest.build_index import main as build_main

        buf = io.StringIO()
        with st.status("수집·재색인 중...", expanded=True) as status:
            with redirect_stdout(buf):
                code = build_main(["--sources", ",".join(sources)], log=print)
            st.code(buf.getvalue()[-5000:] or "(출력 없음)")
            status.update(label="완료" if code == 0 else "일부 실패", state="complete" if code == 0 else "error")
        get_retriever.clear()


def main() -> None:
    st.set_page_config(layout="wide")  # 로그 표가 넓어야 읽기 쉽다 (질의응답 화면은 centered)
    st.title(":material/admin_panel_settings: 관리자")
    if not login_gate():
        return
    with st.sidebar:
        if st.button("로그아웃", icon=":material/logout:", use_container_width=True):
            st.session_state.is_admin = False
            st.rerun()

    try:
        logs, log_error = read_logs(limit=500), None
    except Exception as e:  # Supabase 연결 오류 등
        logs, log_error = [], str(e)

    tab_logs, tab_none, tab_data = st.tabs(["질문 로그", "근거 없음 응답", "데이터 관리"])
    with tab_logs:
        render_logs(logs, log_error)
    with tab_none:
        render_insufficient(logs)
    with tab_data:
        render_data()


main()
