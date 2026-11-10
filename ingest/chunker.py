"""원문을 검색 단위 청크로 나눈다 (PRD §5.3).

- 법령: 조 단위. 긴 조문·별표는 줄 경계에서 나눈다 (별표 표 텍스트는 줄 구조를 그대로 유지).
- KGS Code: 절 번호(예: 1.4, 2.5.3, A1.4) 단위. 더 깊은 번호(1.4.2.1)는 상위 절 안에 둔다.

저장 형식: 문서 1개 = JSON 파일 1개
  data/chunks/law/{law_id}.json, data/chunks/kgs/{code}.json
  {"doc": {...문서 메타·버전...}, "chunks": [Chunk, ...]}
파일명에 버전을 넣지 않아 개정 시 같은 파일이 덮어써진다 (git diff가 작아짐).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from core.models import Chunk

MAX_CHUNK_CHARS = 1800
MIN_CHUNK_CHARS = 120  # 이보다 짧은 KGS 절은 다음 절과 합친다

MAX_HEADING_DEPTH = 4  # 2.5.8.2 까지 절 경계로 쓴다 (그보다 깊은 2.5.8.2.1은 본문)

# KGS 절 제목: "1. 일반사항", "1.4 용어정의", "2.5.3 배관 설치", "A1.4 용어의 정의", "A2  지상식 …"
_KGS_HEADING = re.compile(
    r"^(?P<num>(?:[A-Z]\d{1,2}|\d{1,2})(?:\.\d{1,2}){0,4})(?P<dot>\.)?\s+(?P<title>[가-힣“\"「(\[].{0,80})$"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_long(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """줄 경계에서 max_chars 이하 조각으로 나눈다. 한 줄이 너무 길면 그 줄을 자른다."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        while len(line) > max_chars:
            if buf:
                parts.append("\n".join(buf))
                buf, size = [], 0
            parts.append(line[:max_chars])
            line = line[max_chars:]
        if size + len(line) + 1 > max_chars and buf:
            parts.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return [p for p in parts if p.strip()]


def _with_part(label: str, i: int, n: int) -> str:
    return label if n == 1 else f"{label} ({i}/{n})"


# ── 법령 ───────────────────────────────────────────────────


def chunk_law(record: dict[str, Any]) -> list[Chunk]:
    """law_api 레코드(법령·행정규칙) 1건 → 청크 목록."""
    from ingest.law_api import admrul_url, law_url

    name = record["name"]
    stype = record["source_type"]
    url_for = (lambda label: admrul_url(name)) if stype == "행정규칙" else (lambda label: law_url(name, label))
    common = dict(
        source_type=stype,
        doc_name=name,
        gas_category=record.get("gas_category", "공통"),
        priority=record.get("priority", "A"),
        amended_date=record.get("promulgated_date"),
        collected_at=_now(),
    )
    chunks: list[Chunk] = []
    for art in record.get("articles", []):
        if not art["text"].strip():
            continue
        head = f"{art['label']}({art['title']})" if art.get("title") else art["label"]
        pieces = split_long(art["text"])
        for i, piece in enumerate(pieces, 1):
            chunks.append(
                Chunk(
                    chunk_id=f"law-{record['law_id']}-{art['label']}-{i}",
                    article_or_item=_with_part(head, i, len(pieces)),
                    text=f"[{name}] {head}\n{piece}",
                    url=url_for(art["label"]),
                    effective_date=art.get("effective_date") or record.get("effective_date"),
                    status=art.get("status", "현행"),
                    **common,
                )
            )
    for apx in record.get("appendices", []):
        if not apx["text"].strip():
            continue
        head = f"[{apx['label']}] {apx['title']}".strip()
        pieces = split_long(apx["text"])
        for i, piece in enumerate(pieces, 1):
            chunks.append(
                Chunk(
                    chunk_id=f"law-{record['law_id']}-{apx['label'].replace(' ', '')}-{i}",
                    article_or_item=_with_part(head, i, len(pieces)),
                    text=f"[{name}] {head}\n{piece}",
                    url=url_for(apx["label"]),
                    effective_date=record.get("effective_date"),
                    status="현행",
                    **common,
                )
            )
    return chunks


# ── KGS Code ───────────────────────────────────────────────


def _parse_num(num: str) -> tuple[str, tuple[int, ...]] | None:
    prefix = num[0] if num[0].isalpha() else ""
    try:
        parts = tuple(int(p) for p in (num[1:] if prefix else num).split("."))
    except ValueError:
        return None
    if any(p == 0 for p in parts):
        return None  # "2.0 이하인 것" 같은 본문 숫자를 제목으로 오인하지 않도록
    return prefix, parts


def _is_plausible_next(prev: tuple[str, tuple[int, ...]] | None, cur: tuple[str, tuple[int, ...]]) -> bool:
    """cur가 prev 다음에 올 수 있는 절 번호인지."""
    if prev is None:
        return cur[1][0] <= 2
    if cur[0] != prev[0]:
        # 본문 → 부록 A → 부록 B … 새 부록은 A1/A2부터 시작
        return cur[0] > prev[0] and cur[1][0] <= 2
    p, c = prev[1], cur[1]
    if c <= p:
        return False
    # "2. 제조시설기준(해당 없음)"처럼 하위 절 없는 장은 제목에서 빠지므로 1 → 3 같은 건너뜀을 허용
    return c[0] - p[0] <= 3


def _longest_chain(cands: list[tuple[str, tuple[int, ...]]]) -> set[int]:
    """번호가 일관되게 이어지는 가장 긴 후보 열(인덱스 집합).

    표·목록 속 "3. 크롬함유량", "3.5 이상" 같은 줄이 제목 후보로 섞여도, 문서 전체에서 가장 길게
    이어지는 번호 열만 제목으로 채택하므로 한 번의 오인이 뒤 절 전체를 망치지 않는다.
    """
    n = len(cands)
    if n == 0:
        return set()
    best = [1 if _is_plausible_next(None, c) else 0 for c in cands]
    back = [-1] * n
    for i in range(n):
        for j in range(max(0, i - 400), i):  # 인접 후보 위주로 (긴 문서에서 계산량 제한)
            if best[j] and best[j] + 1 > best[i] and _is_plausible_next(cands[j], cands[i]):
                best[i] = best[j] + 1
                back[i] = j
    end = max(range(n), key=lambda i: (best[i], -i))
    chain: set[int] = set()
    while end != -1 and best[end]:
        chain.add(end)
        end = back[end]
    return chain


def _chapters_with_children(lines: list[str]) -> set[tuple[str, int]]:
    """하위 절(N.1 등)이 실제로 나오는 장 번호. 표 속 "6. 재료명" 같은 목록을 장 제목으로 오인하지 않게 한다."""
    found: set[tuple[str, int]] = set()
    for line in lines:
        m = _KGS_HEADING.match(line)
        parsed = _parse_num(m.group("num")) if m else None
        if parsed and len(parsed[1]) >= 2:
            found.add((parsed[0], parsed[1][0]))
    return found


def find_kgs_sections(pages: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """본문 페이지들을 절 단위로 나눈다. 반환: [{num, title, chapter, page, text}]"""
    lines: list[tuple[int, str]] = [
        (page_no, ln.strip()) for page_no, text in pages for ln in text.splitlines() if ln.strip()
    ]
    real_chapters = _chapters_with_children([ln for _, ln in lines])

    # 1) 제목 후보 수집
    cand_idx: list[int] = []
    cand_num: list[tuple[str, tuple[int, ...]]] = []
    matches: dict[int, re.Match] = {}
    for idx, (_, line) in enumerate(lines):
        m = _KGS_HEADING.match(line)
        parsed = _parse_num(m.group("num")) if m else None
        if parsed is None:
            continue
        depth = len(parsed[1])
        if depth > MAX_HEADING_DEPTH:
            continue  # 더 깊은 번호는 상위 절 안에 둔다
        # 1단계 번호는 "1. 일반사항"처럼 마침표가 있거나 부록 번호(A1)이고, 하위 절이 실제로 있어야 한다
        if depth == 1 and not ((m.group("dot") or parsed[0]) and (parsed[0], parsed[1][0]) in real_chapters):
            continue
        cand_idx.append(idx)
        cand_num.append(parsed)
        matches[idx] = m
    # 2) 가장 길게 이어지는 번호 열만 제목으로 채택
    heading_at = {cand_idx[i]: cand_num[i] for i in _longest_chain(cand_num)}

    # 3) 절 나누기
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    path: dict[int, str] = {}  # 깊이 → 상위 절 제목 (짧은 제목만)
    for idx, (page_no, line) in enumerate(lines):
        parsed = heading_at.get(idx)
        if parsed is not None:
            m = matches[idx]
            depth = len(parsed[1])
            title = m.group("title").strip()
            for d in [d for d in path if d >= depth]:
                del path[d]
            # 상위 절 경로: "2. 시설기준 > 2.5 배관설비 > 2.5.8 배관설비 설치"
            chapter = " > ".join(path[d] for d in sorted(path))
            if len(title) <= 30:  # 문장형 조항(1.4.2.1 이 기준은 …)은 경로에 넣지 않는다
                path[depth] = f"{m.group('num')} {title}"
            current = {
                "num": m.group("num"),
                "title": title,
                "chapter": chapter,
                "page": page_no,
                "lines": [line],
            }
            sections.append(current)
        else:
            if current is None:  # 첫 제목 전의 코드명 등
                current = {"num": "", "title": "", "chapter": "", "page": page_no, "lines": []}
                sections.append(current)
            current["lines"].append(line)
    for s in sections:
        s["text"] = "\n".join(s.pop("lines"))
    return [s for s in sections if s["text"].strip()]


def _merge_short(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for s in sections:
        if merged and len(merged[-1]["text"]) < MIN_CHUNK_CHARS:
            prev = merged[-1]
            prev["text"] += "\n" + s["text"]
            prev["merged_to"] = s["num"]
        else:
            merged.append(dict(s))
    return merged


def chunk_kgs(code: dict[str, Any], pages: list[tuple[int, str]]) -> list[Chunk]:
    """KGS Code 1건(목록 메타 + 본문 페이지) → 청크 목록."""
    doc_name = f"KGS {code['code']}"
    title = f"[KGS {code['code']} {code['year']} {code['name']}]"
    chunks: list[Chunk] = []
    for sec in _merge_short(find_kgs_sections(pages)):
        num = sec["num"]
        short_title = sec["title"] if len(sec["title"]) <= 40 else sec["title"][:40] + "…"
        num_label = num or "머리말"
        if sec.get("merged_to"):
            num_label = f"{num_label}~{sec['merged_to']}"
        label = f"{num_label} {short_title}".strip()
        context = f"{title} {sec['chapter']}".strip() if sec["chapter"] else title
        pieces = split_long(sec["text"])
        for i, piece in enumerate(pieces, 1):
            chunks.append(
                Chunk(
                    chunk_id=f"kgs-{code['pblc_cd']}-{num or '0'}-{i}",
                    source_type="KGS",
                    doc_name=doc_name,
                    article_or_item=_with_part(label, i, len(pieces)),
                    text=f"{context}\n{piece}",
                    url=code["view_url"],
                    effective_date=None,  # 목록에는 개정일만 있다 (시행일은 본문 경과조치 참고)
                    amended_date=code.get("amended_date"),
                    status="현행",
                    gas_category=code.get("gas_category", "공통"),
                    priority="A",
                    collected_at=_now(),
                    page=sec["page"],
                )
            )
    return chunks


# ── 저장·로딩 ──────────────────────────────────────────────


def save_doc(path: Path, doc: dict[str, Any], chunks: list[Chunk]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"doc": doc, "chunks": [c.to_dict() for c in chunks]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(path)


def read_doc_meta(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("doc")
    except (OSError, json.JSONDecodeError):
        return None


def refresh_status(chunk: Chunk, today: date) -> Chunk:
    """시행일 기준으로 현행/시행예정을 다시 계산한다.

    수집 시점에 정한 status를 그대로 쓰면, 시행일이 지나도 문서 버전이 그대로라 재처리되지 않아
    '시행예정'으로 남는다 (REVIEW M4). 그래서 읽을 때마다 오늘(KST) 기준으로 다시 정한다.
    """
    if chunk.effective_date:
        chunk.status = "시행예정" if chunk.effective_date > today.isoformat() else "현행"
    return chunk


def load_chunks(directory: Path, today: date | None = None) -> list[Chunk]:
    """디렉터리(하위 포함)의 모든 청크 파일을 읽는다. 시행예정 여부는 today(기본: 오늘 KST)로 다시 계산."""
    from core.config import today_kst

    today = today or today_kst()
    chunks: list[Chunk] = []
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = data.get("chunks", data) if isinstance(data, dict) else data
        for item in items:
            chunks.append(refresh_status(Chunk.from_dict(item), today))
    return chunks
