"""국가법령정보 공동활용 Open API 수집 (PRD §3.1).

확인 사항 (2026-11):
- 요청·응답 필드명은 open.law.go.kr 가이드(lsNwListGuide, lsNwInfoGuide, admrulListGuide,
  admrulInfoGuide)에서 확인했다.
- 실제 호출은 **신청한 OC + 등록된 서버 IP/도메인**에서만 된다. 미등록 IP에서는
  "사용자 정보 검증에 실패하였습니다" 응답이 온다 (GitHub Actions처럼 IP가 바뀌는 환경 주의).
- 응답의 감싸는 태그 구조는 실제 응답으로 확인하지 못했으므로, XML에서 **필드명 태그를 직접 찾는
  방식**으로 파싱한다 (예: <조문번호>를 자식으로 가진 요소 = 조문 단위).

조문 원문 링크: https://www.law.go.kr/법령/{법령명}/제{n}조[의{m}] (조문 단위로 바로 열림, 확인함)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date
from typing import Any
from urllib.parse import quote

import requests
from lxml import etree

BASE_URL = "https://www.law.go.kr/DRF"
SITE_URL = "https://www.law.go.kr"
REQUEST_INTERVAL_SEC = 0.5
USER_AGENT = "Mozilla/5.0 (compatible; gas-safety-qa-indexer)"


class LawApiError(RuntimeError):
    """API가 오류 응답(인증 실패 등)을 돌려준 경우."""


# ── XML 헬퍼 ──────────────────────────────────────────────


def _norm(tag: str) -> str:
    """가이드의 필드명에는 공백이 섞여 있다('행정규칙 일련번호'). 비교 시 공백을 무시한다."""
    return re.sub(r"\s+", "", tag)


def _text(el: etree._Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _child(el: etree._Element, name: str) -> etree._Element | None:
    target = _norm(name)
    for c in el:
        if isinstance(c.tag, str) and _norm(c.tag) == target:
            return c
    return None


def _child_text(el: etree._Element, name: str) -> str:
    return _text(_child(el, name))


def _units_with(root: etree._Element, *required_children: str) -> list[etree._Element]:
    """주어진 필드들을 모두 직계 자식으로 가진 요소 목록 (문서 순서)."""
    found = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if all(_child(el, name) is not None for name in required_children):
            found.append(el)
    return found


def _fmt_date(yyyymmdd: str) -> str | None:
    digits = re.sub(r"\D", "", yyyymmdd or "")
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def _today() -> date:
    from core.config import today_kst

    return today_kst()


def _status(effective: str | None, today: date | None = None) -> str:
    """수집 시점 표시용. 앱은 읽을 때마다 chunker.refresh_status로 다시 계산한다."""
    if effective and effective > (today or _today()).isoformat():
        return "시행예정"
    return "현행"


def validate_record(rec: dict[str, Any], expected_name: str | None = None) -> dict[str, Any]:
    """응답이 정상 법령 레코드인지 검사한다. XML 형태의 오류 응답("일치하는 법령이 없습니다" 등)이
    빈 레코드로 저장되어 기존 정상 파일을 지우는 일을 막는다 (REVIEW M7)."""
    if not rec.get("name") or not rec.get("law_id"):
        raise LawApiError(f"응답에 법령명/ID가 없습니다 (오류 응답 가능성): {expected_name or ''}")
    if expected_name and rec["name"].replace(" ", "") != expected_name.replace(" ", ""):
        raise LawApiError(f"요청한 법령({expected_name})과 다른 응답({rec['name']})")
    if not rec.get("articles") and not rec.get("appendices"):
        raise LawApiError(f"{rec['name']}: 조문·별표가 하나도 없습니다")
    if rec.get("is_current") is False:
        raise LawApiError(f"{rec['name']}: 현행이 아닌 행정규칙입니다")
    return rec


def _parse_xml(content: bytes) -> etree._Element:
    stripped = content.lstrip()
    if not stripped.startswith(b"<"):
        # 인증 실패 등은 JSON/텍스트로 온다
        raise LawApiError(content.decode("utf-8", errors="replace")[:300])
    root = etree.fromstring(stripped, parser=etree.XMLParser(recover=True, huge_tree=True))
    if root is None:
        raise LawApiError("XML 파싱 실패")
    if root.tag in ("html", "HTML"):
        raise LawApiError(_text(root)[:300])
    return root


def law_url(law_name: str, article_label: str | None = None) -> str:
    """조문 단위 원문 링크. article_label 예: '제17조의2', '별표 7'."""
    path = f"/법령/{law_name.replace(' ', '')}"
    if article_label:
        path += "/" + article_label.replace(" ", "")
    return SITE_URL + quote(path)


def admrul_url(rule_name: str) -> str:
    return SITE_URL + quote(f"/행정규칙/{rule_name.replace(' ', '')}")


def source_type_from_name(name: str) -> str:
    if name.endswith("시행령"):
        return "시행령"
    if name.endswith("시행규칙"):
        return "시행규칙"
    return "법률"


# ── 파서 (테스트 가능하도록 HTTP와 분리) ───────────────────


def parse_law_search(content: bytes) -> list[dict[str, Any]]:
    """lawSearch.do?target=law 응답 → 후보 목록."""
    root = _parse_xml(content)
    results = []
    for el in _units_with(root, "법령ID", "법령명한글"):
        results.append(
            {
                "mst": _child_text(el, "법령일련번호"),
                "law_id": _child_text(el, "법령ID"),
                "name": _child_text(el, "법령명한글"),
                "kind": _child_text(el, "법령구분명"),
                "effective_date": _fmt_date(_child_text(el, "시행일자")),
                "promulgated_date": _fmt_date(_child_text(el, "공포일자")),
                "status_code": _child_text(el, "현행연혁코드"),
            }
        )
    return results


def _article_label(number: str, branch: str) -> str:
    label = f"제{int(number)}조" if number.isdigit() else f"제{number}조"
    if branch and branch.strip("0"):
        label += f"의{int(branch)}"
    return label


def _collect_body(el: etree._Element) -> str:
    """조문내용·항내용·호내용·목내용을 문서 순서대로 이어 붙인다."""
    wanted = {"조문내용", "항내용", "호내용", "목내용"}
    lines = []
    for node in el.iter():
        if isinstance(node.tag, str) and _norm(node.tag) in wanted:
            t = _text(node)
            if t:
                lines.append(t)
    return "\n".join(lines)


def parse_law_service(content: bytes, today: date | None = None) -> dict[str, Any]:
    """lawService.do?target=law 응답 → 법령 레코드 (기본정보 + 조문 + 별표)."""
    root = _parse_xml(content)
    info_el = next(iter(_units_with(root, "법령ID", "법령명_한글")), root)
    name = _child_text(info_el, "법령명_한글")
    effective = _fmt_date(_child_text(info_el, "시행일자"))

    articles = []
    for unit in _units_with(root, "조문번호", "조문내용"):
        if _child_text(unit, "조문여부") == "전문":  # 장·절 제목 줄
            continue
        label = _article_label(_child_text(unit, "조문번호"), _child_text(unit, "조문가지번호"))
        art_eff = _fmt_date(_child_text(unit, "조문시행일자")) or effective
        articles.append(
            {
                "label": label,
                "title": _child_text(unit, "조문제목"),
                "text": _collect_body(unit),
                "effective_date": art_eff,
                "status": _status(art_eff, today),
            }
        )

    appendices = []
    for unit in _units_with(root, "별표번호", "별표내용"):
        number = _child_text(unit, "별표번호").lstrip("0") or "0"
        branch = _child_text(unit, "별표가지번호").lstrip("0")
        kind = _child_text(unit, "별표구분") or "별표"
        label = f"{kind} {number}" + (f"의{branch}" if branch else "")
        appendices.append(
            {
                "label": label,
                "title": _child_text(unit, "별표제목"),
                "text": _child_text(unit, "별표내용"),
            }
        )

    return {
        "law_id": _child_text(info_el, "법령ID"),
        "name": name,
        "source_type": source_type_from_name(name),
        "effective_date": effective,
        "promulgated_date": _fmt_date(_child_text(info_el, "공포일자")),
        "promulgation_no": _child_text(info_el, "공포번호"),
        "articles": articles,
        "appendices": appendices,
    }


def parse_admrul_search(content: bytes) -> list[dict[str, Any]]:
    root = _parse_xml(content)
    results = []
    for el in _units_with(root, "행정규칙일련번호", "행정규칙명"):
        results.append(
            {
                "serial": _child_text(el, "행정규칙일련번호"),
                "rule_id": _child_text(el, "행정규칙ID"),
                "name": _child_text(el, "행정규칙명"),
                "kind": _child_text(el, "행정규칙종류"),
                "ministry": _child_text(el, "소관부처명"),
                "effective_date": _fmt_date(_child_text(el, "시행일자")),
                "status": _child_text(el, "현행연혁구분"),
            }
        )
    return results


def parse_admrul_service(content: bytes, today: date | None = None) -> dict[str, Any]:
    root = _parse_xml(content)
    info_el = next(iter(_units_with(root, "행정규칙명", "시행일자")), root)
    name = _child_text(info_el, "행정규칙명")
    effective = _fmt_date(_child_text(info_el, "시행일자"))
    articles = []
    for node in root.iter():
        if isinstance(node.tag, str) and _norm(node.tag) == "조문내용":
            t = _text(node)
            if not t:
                continue
            m = re.match(r"(제\s*\d+\s*조(?:\s*의\s*\d+)?)\s*(?:\(([^)]*)\))?", t)
            label = re.sub(r"\s+", "", m.group(1)) if m else f"본문{len(articles) + 1}"
            articles.append(
                {
                    "label": label,
                    "title": (m.group(2) or "") if m else "",
                    "text": t,
                    "effective_date": effective,
                    "status": _status(effective, today),
                }
            )
    appendices = [
        {
            "label": f"별표 {_child_text(u, '별표번호').lstrip('0') or '0'}",
            "title": _child_text(u, "별표제목"),
            "text": _child_text(u, "별표내용"),
        }
        for u in _units_with(root, "별표번호", "별표내용")
    ]
    return {
        "law_id": _child_text(info_el, "행정규칙ID") or _child_text(info_el, "행정규칙일련번호"),
        "serial": _child_text(info_el, "행정규칙일련번호"),
        "name": name,
        "source_type": "행정규칙",
        "effective_date": effective,
        "promulgated_date": _fmt_date(_child_text(info_el, "발령일자")),
        "promulgation_no": _child_text(info_el, "발령번호"),
        "is_current": _child_text(info_el, "현행여부") in ("", "Y", "현행"),
        "articles": articles,
        "appendices": appendices,
    }


# ── HTTP ───────────────────────────────────────────────────


class LawApiClient:
    def __init__(self, oc: str, interval: float = REQUEST_INTERVAL_SEC) -> None:
        if not oc:
            raise LawApiError(
                "LAW_API_OC가 설정되지 않았습니다. open.law.go.kr에서 API 사용 신청 후 "
                "발급받은 OC를 secrets.toml 또는 환경변수 LAW_API_OC에 설정하세요."
            )
        self.oc = oc
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.interval = interval
        self._last = 0.0

    def _get(self, endpoint: str, params: dict[str, Any]) -> bytes:
        elapsed = time.monotonic() - self._last
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last = time.monotonic()
        resp = self.session.get(
            f"{BASE_URL}/{endpoint}", params={"OC": self.oc, "type": "XML", **params}, timeout=60
        )
        resp.raise_for_status()
        return resp.content

    def search_law(self, query: str) -> list[dict[str, Any]]:
        return parse_law_search(self._get("lawSearch.do", {"target": "law", "query": query, "display": 100}))

    def fetch_law(self, mst: str, expected_name: str | None = None) -> dict[str, Any]:
        record = parse_law_service(self._get("lawService.do", {"target": "law", "MST": mst}))
        record["mst"] = mst
        return validate_record(record, expected_name)

    def search_admrul(self, query: str) -> list[dict[str, Any]]:
        return parse_admrul_search(
            self._get("lawSearch.do", {"target": "admrul", "query": query, "nw": 1, "display": 100})
        )

    def fetch_admrul(self, serial: str, expected_name: str | None = None) -> dict[str, Any]:
        record = parse_admrul_service(self._get("lawService.do", {"target": "admrul", "ID": serial}))
        return validate_record(record, expected_name)


def pick_current(hits: list[dict[str, Any]], name: str, today: date | None = None) -> dict[str, Any] | None:
    """같은 이름의 검색 결과 중 현행 판을 고른다. 시행일이 미래인 판(시행예정)은 고르지 않는다 (REVIEW M7)."""
    today_s = (today or _today()).isoformat()
    candidates = [
        h
        for h in hits
        if h["name"] == name
        and h.get("status_code", "") in ("", "현행")
        and (not h.get("effective_date") or h["effective_date"] <= today_s)
    ]
    # 시행일이 가장 최근인 판 = 지금 시행 중인 판
    candidates.sort(key=lambda h: h.get("effective_date") or "", reverse=True)
    return candidates[0] if candidates else None


def resolve_targets(
    client: LawApiClient, target: dict[str, Any], today: date | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """target_laws.yaml 항목 1개 → (수집할 법령 후보, 찾지 못한 이름).

    시행령·시행규칙을 함께 수집하도록 했는데 검색에 없으면 '찾지 못한 이름'으로 돌려준다 —
    호출 측이 오류로 처리해 기존 파일이 삭제되지 않게 한다.
    """
    name = target["name"]
    wanted = [name]
    if target.get("include_subordinate"):
        wanted += [f"{name} 시행령", f"{name} 시행규칙"]
    hits = client.search_law(name)
    picked, missing = [], []
    for w in wanted:
        hit = pick_current(hits, w, today)
        (picked.append(hit) if hit else missing.append(w))
    return picked, missing


def collect(targets: dict[str, Any], oc: str, log=print) -> tuple[list[dict[str, Any]], list[str]]:
    """target_laws.yaml 전체를 수집해 (레코드 목록, 오류 메시지 목록)을 반환한다."""
    client = LawApiClient(oc)
    records: list[dict[str, Any]] = []
    errors: list[str] = []

    for target in targets.get("laws", []):
        try:
            found, missing = resolve_targets(client, target)
            for m in missing:
                errors.append(f"현행 법령을 찾지 못함: {m}")
            for hit in found:
                rec = client.fetch_law(hit["mst"], expected_name=hit["name"])
                rec.update(priority=target.get("priority", "A"), gas_category=target.get("gas_category", "공통"))
                records.append(rec)
                log(f"  [법령] {rec['name']} — 조문 {len(rec['articles'])}, 별표 {len(rec['appendices'])}")
        except (LawApiError, requests.RequestException) as e:
            errors.append(f"{target['name']}: {e}")
            if isinstance(e, LawApiError) and "검증" in str(e):
                break  # 인증 실패면 나머지도 실패하므로 중단

    for rule in targets.get("admin_rules", []) or []:
        name = rule["name"] if isinstance(rule, dict) else rule
        try:
            hits = [
                h
                for h in client.search_admrul(name)
                if h["name"] == name and h.get("status", "") in ("", "현행")
            ]
            if not hits:
                errors.append(f"현행 행정규칙을 찾지 못함: {name}")
                continue
            hits.sort(key=lambda h: h.get("effective_date") or "", reverse=True)
            rec = client.fetch_admrul(hits[0]["serial"], expected_name=name)
            rec.update(
                priority="B",
                gas_category=(rule.get("gas_category") if isinstance(rule, dict) else None) or "공통",
            )
            records.append(rec)
            log(f"  [행정규칙] {rec['name']} — 조문 {len(rec['articles'])}")
        except (LawApiError, requests.RequestException) as e:
            errors.append(f"{name}: {e}")
    return records, errors


def main(argv: list[str] | None = None) -> int:
    """행정규칙 후보 검색 도구: python -m ingest.law_api --search-admrul"""
    import yaml

    from core.config import TARGET_LAWS_PATH, get_secret

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="국가법령정보 Open API 도구")
    parser.add_argument("--search-admrul", action="store_true", help="admin_rule_search 키워드로 행정규칙 후보 출력")
    args = parser.parse_args(argv)
    if not args.search_admrul:
        parser.print_help()
        return 0
    try:
        client = LawApiClient(get_secret("LAW_API_OC") or "")
        cfg = yaml.safe_load(TARGET_LAWS_PATH.read_text(encoding="utf-8"))
        seen = set()
        for kw in cfg.get("admin_rule_search", {}).get("keywords", []):
            for h in client.search_admrul(kw):
                if h["serial"] in seen:
                    continue
                seen.add(h["serial"])
                print(f"{h['name']}\t{h['kind']}\t{h['ministry']}\t시행 {h['effective_date']}")
    except LawApiError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    print("\n위 목록에서 필요한 것을 config/target_laws.yaml의 admin_rules에 적으세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
