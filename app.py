"""가스안전 Q&A — Streamlit Cloud 진입점 (페이지 구성만 담당).

화면: views/chat.py (질의응답), views/admin.py (관리자)
"""

from __future__ import annotations

import streamlit as st

from ui.styles import APP_ICON, APP_TITLE, apply_styles

st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON, layout="centered")
apply_styles()

page = st.navigation(
    [
        st.Page("views/chat.py", title="질의응답", icon=":material/forum:", default=True),
        st.Page("views/admin.py", title="관리자", icon=":material/admin_panel_settings:", url_path="admin"),
    ]
)
page.run()
