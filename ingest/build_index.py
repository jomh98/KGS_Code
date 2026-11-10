"""수집 → 청킹 → 색인 전체 실행 진입점.

사용법:
  python -m ingest.build_index                 # 법령 + KGS Code
  python -m ingest.build_index --sources kgs   # KGS Code만 (OC 없이 가능)
  python -m ingest.build_index --sources law   # 법령만 (LAW_API_OC + 등록된 IP 필요)
  python -m ingest.build_index --force         # 바뀌지 않은 문서도 다시 처리
  python -m ingest.build_index --limit 5       # 개발용: 문서 5개만

동작:
- 문서별로 버전(KGS: pblcCd, 법령: 법령일련번호 MST)을 기존 청크 파일과 비교해 바뀐 것만 다시 처리
- 현행 목록에 없는 문서(폐지·효력만료·대상 제외)의 청크 파일은 삭제 → 색인에 옛 기준이 남지 않음
- 삭제 안전장치 (REVIEW M2): 목록 조회가 실패하거나 분야가 통째로 비면 삭제하지 않고 종료 코드 1.
  한 번에 기존 문서의 10%(최소 3건)를 넘게 지우려 하면 삭제를 멈추고 종료 코드 1 (사람이 확인)
- 문서 하나가 실패해도 나머지 갱신은 반영한다(종료 코드 0). 실패한 문서는 이전 판을 유지하되
  meta.json의 kgs_stale에 기록해 화면에 '최신판 미반영'으로 표시한다
- 새 판 PDF에 텍스트가 없으면(스캔본) 이전 판 청크를 삭제한다 — 구판이 '현행'으로 남지 않게 (REVIEW M3)
- GitHub Actions에서는 경고를 ::warning:: 로 출력해 실행 요약에 보이게 한다
- EMBEDDING_API_KEY가 있으면 바뀐 청크만 임베딩해 data/index/embeddings.npz 갱신
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from core.config import (
    CHUNKS_DIR,
    INDEX_DIR,
    META_PATH,
    RAW_DIR,
    TARGET_LAWS_PATH,
    get_secret,
    load_meta,
    today_kst,
)
from ingest import chunker

MAX_DELETE_RATIO = 0.10
MIN_DELETE_GUARD = 3


def _today() -> str:
    return today_kst().isoformat()


def warn(msg: str, log=print) -> None:
    """GitHub Actions 실행 요약에 경고로 보이게 한다."""
    log(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"[경고] {msg}")


def safe_delete(out_dir: Path, keep: set[str], label: str, log=print) -> tuple[list[str], str | None]:
    """keep에 없는 청크 파일을 지운다. 너무 많이 지우려 하면 멈추고 오류 메시지를 돌려준다."""
    existing = list(out_dir.glob("*.json"))
    doomed = [p for p in existing if p.stem not in keep]
    limit = max(MIN_DELETE_GUARD, int(len(existing) * MAX_DELETE_RATIO))
    if len(doomed) > limit:
        names = ", ".join(sorted(p.stem for p in doomed)[:20])
        return [], (
            f"{label}: 기존 {len(existing)}건 중 {len(doomed)}건을 삭제하려 해 중단했습니다 "
            f"(한도 {limit}건). 목록 조회가 비정상일 수 있으니 확인 후 수동으로 처리하세요: {names}"
        )
    for p in doomed:
        p.unlink()
    return [p.stem for p in doomed], None


def update_kgs(force: bool = False, limit: int | None = None, log=print) -> dict[str, Any]:
    from ingest.kgs_crawler import KgsClient, download_pdf, extract_pages, list_codes

    out_dir = CHUNKS_DIR / "kgs"
    client = KgsClient()
    codes = list_codes(client)
    if not codes:
        raise RuntimeError("KGS Code 목록이 비어 있습니다 (사이트 구조 변경 가능성)")
    log(f"[KGS] 현행 코드 {len(codes)}건")

    # 같은 코드번호가 여러 분야에 있으면 첫 번째만 사용
    by_code: dict[str, dict[str, Any]] = {}
    for c in codes:
        by_code.setdefault(c["code"], c)
    targets = list(by_code.values())[:limit] if limit else list(by_code.values())

    updated = skipped = 0
    errors: list[str] = []  # 커밋을 막아야 하는 오류 (삭제 중단 등)
    failed: list[str] = []  # 문서 단위 실패 — 나머지는 반영
    stale: list[str] = []  # 새 판 반영에 실패해 이전 판이 남아 있는 코드
    no_text: list[str] = []
    for c in targets:
        path = out_dir / f"{c['code']}.json"
        meta = chunker.read_doc_meta(path)
        if meta and meta.get("version") == c["pblc_cd"] and not force:
            skipped += 1
            continue
        try:
            pdf = download_pdf(c, RAW_DIR / "kgs", client)
            pages = extract_pages(pdf)
            chunks = chunker.chunk_kgs(c, pages)
            if not chunks:
                # 본문이 스캔 이미지라 텍스트가 없는 PDF — OCR 없이는 처리 불가.
                no_text.append(c["code"])
                if path.exists() and meta and meta.get("version") != c["pblc_cd"]:
                    path.unlink()  # 이전 판이 '현행'으로 남지 않게 (REVIEW M3)
                    updated += 1
                    warn(f"KGS {c['code']} 새 판({c['pblc_cd']})이 스캔본이라 이전 판 청크를 삭제했습니다", log)
                else:
                    log(f"  [KGS] 텍스트 없음(스캔본) {c['code']} {c['name'][:30]} — 건너뜀")
                continue
            chunker.save_doc(path, {**c, "version": c["pblc_cd"], "kind": "kgs"}, chunks)
            updated += 1
            log(f"  [KGS] {c['code']} {c['name'][:30]} — 청크 {len(chunks)}")
        except Exception as e:  # 한 코드 실패가 전체를 멈추지 않게
            failed.append(f"KGS {c['code']}: {e}")
            if path.exists():
                stale.append(c["code"])
            warn(f"KGS {c['code']} 처리 실패 — {'이전 판 유지' if path.exists() else '색인 없음'}: {e}", log)

    removed: list[str] = []
    if not limit:  # 일부만 처리한 경우엔 삭제하지 않는다
        removed, guard_error = safe_delete(out_dir, set(by_code), "KGS", log)
        if guard_error:
            errors.append(guard_error)
            warn(guard_error, log)
        elif removed:
            log(f"[KGS] 현행 목록에 없어 삭제: {', '.join(removed)}")
    log(
        f"[KGS] 갱신 {updated}, 변경 없음 {skipped}, 삭제 {len(removed)}, "
        f"텍스트 없음 {len(no_text)}, 실패 {len(failed)}"
    )
    return {
        "updated": updated,
        "skipped": skipped,
        "removed": removed,
        "errors": errors,
        "failed": failed,
        "stale": stale,
        "no_text": no_text,
    }


def update_law(force: bool = False, limit: int | None = None, log=print) -> dict[str, Any]:
    from ingest.law_api import collect

    oc = get_secret("LAW_API_OC")
    if not oc:
        raise RuntimeError(
            "LAW_API_OC가 없습니다. open.law.go.kr에서 Open API 사용 신청(서버 IP 등록 포함) 후 "
            ".streamlit/secrets.toml 또는 환경변수 LAW_API_OC에 설정하세요."
        )
    targets = yaml.safe_load(TARGET_LAWS_PATH.read_text(encoding="utf-8"))
    if limit:
        targets["laws"] = targets.get("laws", [])[:limit]
    records, errors = collect(targets, oc, log=log)
    out_dir = CHUNKS_DIR / "law"
    updated = skipped = 0
    keep: set[str] = set()
    for rec in records:
        file_id = f"{rec['source_type']}-{rec['law_id']}"
        keep.add(file_id)
        path = out_dir / f"{file_id}.json"
        version = rec.get("mst") or rec.get("serial") or rec.get("promulgation_no")
        meta = chunker.read_doc_meta(path)
        if meta and meta.get("version") == version and not force:
            skipped += 1
            continue
        chunks = chunker.chunk_law(rec)
        doc = {k: v for k, v in rec.items() if k not in ("articles", "appendices")}
        chunker.save_doc(path, {**doc, "version": version, "kind": "law"}, chunks)
        updated += 1

    removed: list[str] = []
    # 하나라도 오류가 있으면(대상을 못 찾음, 응답 검증 실패 등) 삭제하지 않는다 — 정상 문서를 지우지 않게 (REVIEW M7)
    if records and not errors and not limit:
        removed, guard_error = safe_delete(out_dir, keep, "법령", log)
        if guard_error:
            errors.append(guard_error)
    log(f"[법령] 갱신 {updated}, 변경 없음 {skipped}, 삭제 {len(removed)}, 오류 {len(errors)}")
    for e in errors:
        log(f"  [법령] {e}")
    return {"updated": updated, "skipped": skipped, "removed": removed, "errors": errors}


def update_vectors(log=print) -> int | None:
    from core.embeddings import get_embedder
    from core.retriever import build_vectors

    embedder = get_embedder()
    if embedder is None:
        log("[임베딩] EMBEDDING_API_KEY 없음 → 건너뜀 (앱은 키워드 검색만 사용)")
        return None
    chunks = chunker.load_chunks(CHUNKS_DIR)
    return build_vectors(INDEX_DIR, chunks, embedder, log=log)


def write_meta(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    meta = load_meta()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if "kgs" in results and not results["kgs"].get("fatal"):
        meta["kgs_updated_at"] = _today()
        meta["kgs_no_text"] = results["kgs"].get("no_text", [])  # 스캔본이라 색인하지 못한 코드
        meta["kgs_stale"] = results["kgs"].get("stale", [])  # 새 판 반영 실패, 이전 판 유지 중
        meta["kgs_failed"] = results["kgs"].get("failed", [])
    if "law" in results and not results["law"].get("fatal") and not results["law"].get("errors"):
        meta["law_updated_at"] = _today()
    counts: dict[str, int] = {}
    docs = 0
    for path in CHUNKS_DIR.rglob("*.json"):
        docs += 1
        try:
            for c in json.loads(path.read_text(encoding="utf-8"))["chunks"]:
                counts[c["source_type"]] = counts.get(c["source_type"], 0) + 1
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    changed = any(r.get("updated") or r.get("removed") for r in results.values())
    meta.update(chunk_count=sum(counts.values()), chunk_count_by_source=counts, doc_count=docs)
    # index_built_at은 앱 캐시 키(data_version)로 쓰이므로 문서가 실제로 바뀐 경우에만 갱신
    if changed or not meta.get("index_built_at"):
        meta["index_built_at"] = now
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def main(argv: list[str] | None = None, log=print) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="법령·KGS Code 수집 및 색인")
    parser.add_argument("--sources", default="law,kgs", help="law,kgs 중 쉼표로 구분")
    parser.add_argument("--force", action="store_true", help="변경 없는 문서도 다시 처리")
    parser.add_argument("--limit", type=int, default=None, help="개발용: 소스별 문서 수 제한")
    args = parser.parse_args(argv)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    results: dict[str, dict[str, Any]] = {}
    failed = False
    for src, fn in (("law", update_law), ("kgs", update_kgs)):
        if src not in sources:
            continue
        try:
            results[src] = fn(force=args.force, limit=args.limit, log=log)
            failed |= bool(results[src]["errors"])
        except Exception as e:  # 목록 조회 실패·분야 비어 있음(KgsListError)·인증 실패 등 → 삭제 없이 종료 코드 1
            warn(f"[{src}] 실패: {e}", log)
            results[src] = {"fatal": str(e), "errors": [str(e)]}
            failed = True

    try:
        update_vectors(log=log)
    except Exception as e:
        log(f"[임베딩] 실패: {e}")
        failed = True

    meta = write_meta(results)
    log(f"[완료] 문서 {meta['doc_count']}, 청크 {meta['chunk_count']} {meta['chunk_count_by_source']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
