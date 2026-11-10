"""근거 게이트(F-06) 평가 — 실데이터 색인으로 tests/gate_questions.yaml 을 돌린다.

사용법: python -m scripts.eval_gate [--threshold 0.2] [--verbose]
종료 코드: 관련 질문이 막히거나 무관한 질문이 통과하면 1 (CI에서 사용 가능)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from core.config import CHUNKS_DIR, ROOT, get_setting
from core.retriever import Retriever, domain_tokens

QUESTIONS = ROOT / "tests" / "gate_questions.yaml"


def evaluate(retriever: Retriever, questions: dict, threshold: float) -> dict:
    rows = []
    for label in ("relevant", "unrelated"):
        for q in questions.get(label, []):
            res = retriever.search(q, top_k=get_setting("TOP_K"))
            cov = max((x.coverage for x in res), default=0.0)
            passed = cov >= threshold
            ok = passed if label == "relevant" else not passed
            top = res[0].chunk.citation if res else "-"
            rows.append({"label": label, "q": q, "cov": cov, "ok": ok, "top": top,
                         "terms": domain_tokens(q, retriever.lexicon)})
    return {
        "rows": rows,
        "relevant_ok": sum(r["ok"] for r in rows if r["label"] == "relevant"),
        "relevant_n": sum(r["label"] == "relevant" for r in rows),
        "unrelated_ok": sum(r["ok"] for r in rows if r["label"] == "unrelated"),
        "unrelated_n": sum(r["label"] == "unrelated" for r in rows),
    }


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    threshold = args.threshold if args.threshold is not None else get_setting("MIN_RETRIEVAL_SCORE")

    retriever = Retriever.from_directory(CHUNKS_DIR)
    if not retriever.chunks:
        print("색인된 청크가 없습니다. python -m ingest.build_index 먼저 실행하세요.")
        return 2
    result = evaluate(retriever, yaml.safe_load(args.questions.read_text(encoding="utf-8")), threshold)
    for r in result["rows"]:
        if args.verbose or not r["ok"]:
            mark = "OK  " if r["ok"] else "FAIL"
            print(f"{mark} [{r['label']:9}] {r['cov']:.2f}  {r['q'][:50]}  → {r['top'][:50]}  {r['terms'][:6]}")
    print(
        f"\n문턱값 {threshold:.2f} | 관련 질문 통과 {result['relevant_ok']}/{result['relevant_n']} | "
        f"무관한 질문 차단 {result['unrelated_ok']}/{result['unrelated_n']}"
    )
    all_ok = result["relevant_ok"] == result["relevant_n"] and result["unrelated_ok"] == result["unrelated_n"]
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
