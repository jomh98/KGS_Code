"""KGS Code 수집 (PRD §3.2).

사이트 구조 (2026-11 확인):
- 목록: GET /kgscode.codeSearch.listV2.ex.do?pblcRlmCd={분야}&pubEnd01=F&pageIndex={n}
  · pubEnd01=F → '현행' 코드만 조회 (효력만료·폐지 제외)
  · 분야: 01 고압가스, 02 액화석유가스, 03 도시가스, 05 수소, 04 공통
  · 표 열: 번호 | 분류 | 코드번호/코드명 | 개정일 | 페이지 | 조회수 | 보기(PDF)
- PDF: POST /cmm/fms/kgsFileDown.ex.do  (file_nm=kgscode_pdf/2026/AC116_260911.pdf, file_folder=codeLink)
  · 로그인 없이 내려받을 수 있음
- 코드 상세: /kgscode.codeSearch.view.ex.do?pblcCd=AC116_260911
  · pblcCd = 코드번호_개정일(YYMMDD) → 버전 식별자로 사용
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

BASE = "https://cyber.kgs.or.kr"
INDEX_URL = f"{BASE}/kgscode.Index.do"
LIST_URL = f"{BASE}/kgscode.codeSearch.listV2.ex.do"
DOWNLOAD_URL = f"{BASE}/cmm/fms/kgsFileDown.ex.do"
VIEW_URL = f"{BASE}/kgscode.codeSearch.view.ex.do"
REQUEST_INTERVAL_SEC = 1.0
USER_AGENT = "Mozilla/5.0 (compatible; gas-safety-qa-indexer)"

# 사이트 분야 코드 → 앱의 gas_category
CATEGORIES: dict[str, str] = {
    "01": "고압가스",
    "02": "LPG",
    "03": "도시가스",
    "05": "수소",
    "04": "공통",
}

_CODE_RE = re.compile(r"KGS\s+([A-Z]{2}\d{3})\s+(\d{4})")
_PBLC_RE = re.compile(r"pblcCd=([A-Z]{2}\d{3}_\d{6})")
_FILE_RE = re.compile(r"file_nm=([^&'\"]+\.pdf)")
# 머리글 변형: "KGS AC116 2026", "KGS S AA012 2026", "KGS GC1012026"
_HEADER_RE = re.compile(r"^KGS\s*(?:[A-Z]\s+)?[A-Z]{2}\s*\d{3}\s*\d{4}$")


class KgsClient:
    """요청 간격을 지키는 세션 래퍼."""

    def __init__(self, interval: float = REQUEST_INTERVAL_SEC) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.interval = interval
        self._last = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last = time.monotonic()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self._wait()
        resp = self.session.get(url, timeout=60, **kwargs)
        resp.raise_for_status()
        return resp

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        self._wait()
        resp = self.session.post(url, timeout=300, **kwargs)
        resp.raise_for_status()
        return resp


def parse_list_page(html: str, category_code: str) -> list[dict[str, Any]]:
    """목록 페이지 HTML에서 코드 행을 파싱한다."""
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        title_td = tds[2]
        title_text = title_td.get_text(" ", strip=True)
        m = _CODE_RE.search(title_text)
        link = title_td.find("a")
        pblc = _PBLC_RE.search(link.get("href", "")) if link else None
        file_m = _FILE_RE.search(str(tds[6]))
        if not (m and pblc):
            continue
        name_tag = title_td.find("p")
        name = name_tag.get_text(" ", strip=True) if name_tag else title_text[m.end():].strip()
        amended = tds[3].get_text(strip=True).replace(".", "-")  # 2026.09.11 → 2026-09-11
        pages = tds[4].get_text(strip=True)
        rows.append(
            {
                "code": m.group(1),
                "year": m.group(2),
                "name": re.sub(r"\s+", " ", name),
                "category_code": category_code,
                "gas_category": CATEGORIES.get(category_code, "공통"),
                "classification": tds[1].get_text(" ", strip=True).replace("\xa0", " "),
                "amended_date": amended,
                "pages": int(pages) if pages.isdigit() else None,
                "pblc_cd": pblc.group(1),
                "file_nm": file_m.group(1) if file_m else None,
                "view_url": f"{VIEW_URL}?pblcCd={pblc.group(1)}",
            }
        )
    return rows


class KgsListError(RuntimeError):
    """목록을 믿을 수 없을 때 (분야가 통째로 비었거나 페이지가 끝나지 않음). 삭제를 막기 위해 예외로 처리."""


MAX_PAGES_PER_CATEGORY = 100


def list_codes(client: KgsClient | None = None, categories: list[str] | None = None) -> list[dict[str, Any]]:
    """현행 KGS Code 목록(메타데이터)을 반환한다. pblc_cd 기준으로 중복 제거.

    분야 하나라도 0건이면 KgsListError — 사이트 점검·구조 변경으로 빈 표가 와도 그 분야 청크가
    통째로 삭제되지 않게 한다 (REVIEW M2).
    """
    client = client or KgsClient()
    seen: dict[str, dict[str, Any]] = {}
    for cat in categories or list(CATEGORIES):
        cat_seen: set[str] = set()
        prev_page: set[str] = set()
        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            resp = client.get(LIST_URL, params={"pblcRlmCd": cat, "pubEnd01": "F", "pageIndex": page})
            rows = parse_list_page(resp.text, cat)
            ids = {r["pblc_cd"] for r in rows}
            # 마지막 페이지를 넘으면 빈 표이거나 같은 페이지가 반복된다.
            # (다른 분야에서 이미 본 코드인지로 판단하면 공통 분야처럼 겹치는 분야를 중간에 끊게 된다)
            if not rows or ids == prev_page or ids <= cat_seen:
                break
            prev_page = ids
            cat_seen |= ids
            for r in rows:
                seen.setdefault(r["pblc_cd"], r)
        else:
            raise KgsListError(f"분야 {cat}: {MAX_PAGES_PER_CATEGORY}쪽을 넘어도 목록이 끝나지 않음")
        if not cat_seen:
            raise KgsListError(f"분야 {cat}({CATEGORIES.get(cat, cat)}) 목록이 비어 있음 — 사이트 점검·구조 변경 가능성")
    return list(seen.values())


def download_pdf(code: dict[str, Any], dest_dir: Path, client: KgsClient | None = None) -> Path:
    """코드 PDF를 내려받아 경로를 반환한다. 이미 있으면 다시 받지 않는다."""
    if not code.get("file_nm"):
        raise ValueError(f"PDF 링크 없음: {code['pblc_cd']}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{code['pblc_cd']}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path
    client = client or KgsClient()
    resp = client.post(DOWNLOAD_URL, data={"file_nm": code["file_nm"], "file_folder": "codeLink"})
    if not resp.content.startswith(b"%PDF"):
        raise ValueError(f"PDF가 아닌 응답: {code['pblc_cd']} ({resp.headers.get('content-type')})")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return path


def extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    """본문 페이지의 (쪽번호, 텍스트) 목록. 표지·위원회·이력·목차 페이지는 제외한다.

    본문 페이지는 첫 줄이 'KGS AC116 2026' 머리글, 다음 줄이 아라비아 숫자 쪽번호다.
    머리글과 쪽번호 줄은 제거한다.
    """
    import pymupdf

    pages: list[tuple[int, str]] = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            lines = [ln.rstrip() for ln in page.get_text().splitlines()]
            nonempty = [ln.strip() for ln in lines if ln.strip()]
            if len(nonempty) < 2 or not _HEADER_RE.match(nonempty[0]) or not nonempty[1].isdigit():
                continue
            page_no = int(nonempty[1])
            # 머리글·쪽번호 두 줄을 뺀 나머지
            body_lines = []
            skipped = 0
            for ln in lines:
                if skipped < 2 and ln.strip():
                    skipped += 1
                    continue
                body_lines.append(ln)
            pages.append((page_no, "\n".join(body_lines).strip()))
    return pages
