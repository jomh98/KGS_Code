"""화면 상수와 커스텀 CSS.

디자인은 .streamlit/config.toml 테마와 Streamlit 기본 컴포넌트(st.badge, 테두리 컨테이너 등)로 만든다.
unsafe_allow_html을 쓰는 곳은 이 파일의 apply_styles() 한 곳뿐이며, 사용자 입력은 절대 넣지 않는다.
CSS에는 색을 쓰지 않아(테마 색을 그대로 사용) 라이트·다크 모드 모두에서 깨지지 않는다.
"""

from __future__ import annotations

import streamlit as st

APP_TITLE = "가스안전 Q&A"
APP_ICON = "🛡️"
ASSISTANT_AVATAR = "🛡️"
USER_AVATAR = "🙋"

# 문서 유형별 배지 색 (주황·빨강은 경고용으로 남겨 둔다)
SOURCE_BADGE = {
    "법률": "blue",
    "시행령": "violet",
    "시행규칙": "green",
    "행정규칙": "gray",
    "KGS": "primary",
}
SOURCE_LABEL = {"KGS": "KGS Code"}

GAS_ICONS = {
    "고압가스": ":material/propane_tank:",
    "LPG": ":material/local_fire_department:",
    "도시가스": ":material/gas_meter:",
    "수소": ":material/bolt:",
}

_CSS = """
<style>
/* 근거 원문: 줄바꿈 유지 + 읽기 편한 크기 */
[data-testid="stText"] { font-size: 0.9rem; line-height: 1.6; }
/* 채팅 메시지 사이 여백 */
[data-testid="stChatMessage"] { margin-bottom: 0.25rem; }
</style>
"""


def apply_styles() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def source_badge(source_type: str) -> str:
    """markdown 배지 문법 (예: ':blue-badge[법률]')."""
    color = SOURCE_BADGE.get(source_type, "gray")
    return f":{color}-badge[{SOURCE_LABEL.get(source_type, source_type)}]"
