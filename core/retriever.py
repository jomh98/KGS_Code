"""근거 검색 (F-02, PRD §5.2).

- 키워드 검색: BM25. 한국어는 조사가 붙어 단어가 달라지므로("배관을", "배관의")
  한글은 2글자 단위(bigram)로, 영문·숫자는 단어 단위로 토큰화한다.
  색인은 앱 시작 시 청크에서 바로 만든다 (수만 청크 기준 수 초, numpy 희소 구조).
- 벡터 검색: data/index/embeddings.npz와 임베딩 키가 모두 있을 때만 사용.
- 두 결과는 순위 융합(RRF)으로 합치고, 우선순위 가중치(A > B > C)를 곱한다.
- coverage(근거 부족 판단 F-06): 질문 속 **가스 분야 용어**(idf 가중) 중 청크에 들어 있는 비율.
  분야 용어집은 KGS 코드명·법령명에서 자동으로 만든다. 질문 전체 토큰으로 계산하면 "회사·공장·식당"
  같은 말이 분모를 키워 문장형 질문이 막히고, 분야 용어가 하나도 없는 질문("서울 날씨")은 0이 된다
  (REVIEW.md C1). 관련성의 세부 판단은 LLM 프롬프트 규칙 3에 맡긴다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.models import Chunk, RetrievedChunk

PRIORITY_WEIGHT = {"A": 1.0, "B": 0.9, "C": 0.75}
MAX_CHUNKS_PER_DOC = 3  # 한 문서가 결과를 독차지하지 않도록
RRF_K = 60
CANDIDATES = 60

_HANGUL = re.compile(r"[가-힣]+")
_ALNUM = re.compile(r"[a-z]+\d*|\d+(?:\.\d+)?")

# 질문에 흔히 붙는 말 — 검색어로서 의미가 거의 없다
_QUERY_STOP = {
    "기준", "준은", "무엇", "엇인", "인가", "인지", "알려", "려줘", "려주", "주세", "세요",
    "어떻", "떻게", "하나", "나요", "되나", "는지", "합니", "니까", "있나", "해야", "야하",
    "경우", "관련", "대해", "해서", "에서", "으로", "하는",
}


_HANGUL_SPACED = re.compile(r"([가-힣])\s([가-힣])")

# 질문에 가스 종류가 나오면 그 분류의 근거를 우대한다
_GAS_HINTS = {
    "LPG": ("lpg", "액화석유가스", "엘피지", "프로판", "부탄"),
    "도시가스": ("도시가스", "lng"),
    "수소": ("수소",),
    "고압가스": ("고압가스",),
}
GAS_BOOST = 1.3

# 일상 표현 → 기준 문서 표현 (KGS 본문은 "LPG" 대신 "액화석유가스"라고 쓴다)
_SYNONYMS = {
    "lpg": "액화석유가스",
    "엘피지": "액화석유가스",
    "프로판": "액화석유가스",
    "부탄": "액화석유가스",
    "lng": "액화천연가스 도시가스",
    "cng": "압축천연가스",
}
_SYNONYM_RE = re.compile("|".join(map(re.escape, _SYNONYMS)), re.IGNORECASE)

# 분야 용어집에서 뺄 만큼 흔한 제목 용어의 비율 (거의 모든 코드명에 들어 있는 "기준·시설·검사" 등)
_LEXICON_MAX_TITLE_RATIO = 0.3
_LEXICON_MIN_TITLES = 2
_LEXICON_ALWAYS = {"가스"}
_KGS_TITLE = re.compile(r"^\[KGS \S+ \d{4} ([^\]]+)\]")


def tokenize(text: str, bridge: bool = True) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    for run in _HANGUL.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    # 띄어쓰기 차이("용기 보관실" ↔ "용기보관실")를 메우는 어절 경계 bigram
    if bridge:
        tokens.extend(a + b for a, b in _HANGUL_SPACED.findall(text))
    tokens.extend(_ALNUM.findall(text))
    return tokens


def expand_synonyms(text: str) -> str:
    """질문에 기준 문서식 표현을 덧붙인다 (원문은 유지)."""
    extra = {_SYNONYMS[m.group(0).lower()] for m in _SYNONYM_RE.finditer(text)}
    return f"{text} {' '.join(sorted(extra))}" if extra else text


def build_lexicon(titles: list[str]) -> set[str]:
    """문서 제목(KGS 코드명·법령명)에서 가스 분야 용어집을 만든다."""
    n = len(titles)
    if n == 0:
        return set(_LEXICON_ALWAYS)
    df: Counter = Counter()
    for t in titles:
        df.update(set(tokenize(t, bridge=False)))
    lexicon = {
        tok
        for tok, k in df.items()
        if k >= min(_LEXICON_MIN_TITLES, n) and k / n <= _LEXICON_MAX_TITLE_RATIO and len(tok) >= 2
    }
    return lexicon | _LEXICON_ALWAYS


def doc_title(chunk: Chunk) -> str:
    m = _KGS_TITLE.match(chunk.text) if chunk.source_type == "KGS" else None
    return m.group(1) if m else chunk.doc_name


def chunk_gas_tags(chunk: Chunk) -> set[str]:
    """청크의 가스 분야: 사이트 분류 + 제목에 나온 가스 종류.

    KGS 사이트는 수소충전(FP216·FP217)을 '고압가스'로 분류하므로, 분류만 보면 수소 질문에 맞는
    근거를 '다른 분야'로 잘못 표시하거나 수소 필터에서 빠뜨린다.
    """
    return {chunk.gas_category} | gas_hints(doc_title(chunk))


def gas_hints(query: str) -> set[str]:
    q = query.lower()
    return {cat for cat, words in _GAS_HINTS.items() if any(w in q for w in words)}


def index_text(chunk: Chunk) -> str:
    """색인용 텍스트: 절 제목을 한 번 더 넣어 제목에 나온 단어에 가중치를 준다."""
    return f"{chunk.text}\n{chunk.article_or_item}\n{chunk.article_or_item}"


def query_tokens(text: str) -> list[str]:
    text = expand_synonyms(text)
    toks = [t for t in tokenize(text) if t not in _QUERY_STOP]
    return list(dict.fromkeys(toks)) or list(dict.fromkeys(tokenize(text)))


def domain_tokens(text: str, lexicon: set[str]) -> list[str]:
    """질문 속 가스 분야 용어 (어절 경계 bigram 제외 — "이전 지시"의 '전지' 같은 우연 일치 방지)."""
    toks = tokenize(expand_synonyms(text), bridge=False)
    return list(dict.fromkeys(t for t in toks if t in lexicon and t not in _QUERY_STOP))


@dataclass
class SearchFilters:
    """사이드바 검색 범위 (F-10). 빈 리스트는 '전체'를 뜻한다.

    가스 종류를 고르면 '공통' 분류(공통 KGS Code, 관련 법령)는 항상 함께 검색한다.
    """

    gas_categories: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)

    def key(self) -> str:
        return f"{sorted(self.gas_categories)}|{sorted(self.source_types)}"


class BM25Index:
    """용어별 (문서번호, 가중치) 배열을 미리 계산한 BM25. 질의는 해당 용어 행만 더한다."""

    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75) -> None:
        self.n = len(docs)
        vocab: dict[str, int] = {}
        term_ids: list[int] = []
        doc_idx: list[int] = []
        tfs: list[int] = []
        lengths = np.zeros(self.n, dtype=np.float32)
        for i, d in enumerate(docs):
            toks = tokenize(d)
            lengths[i] = len(toks)
            for t, tf in Counter(toks).items():
                j = vocab.get(t)
                if j is None:
                    j = vocab[t] = len(vocab)
                term_ids.append(j)
                doc_idx.append(i)
                tfs.append(tf)
        self.vocab = vocab
        terms = np.asarray(term_ids, dtype=np.int32)
        dids = np.asarray(doc_idx, dtype=np.int32)
        tf_arr = np.asarray(tfs, dtype=np.float32)

        df = np.bincount(terms, minlength=len(vocab)).astype(np.float32)
        self.idf = np.log1p((self.n - df + 0.5) / (df + 0.5)).astype(np.float32)
        avgdl = float(lengths.mean()) if self.n else 1.0
        norm = k1 * (1 - b + b * lengths[dids] / max(avgdl, 1e-6))
        weights = self.idf[terms] * tf_arr * (k1 + 1) / (tf_arr + norm)

        # 용어별 posting 목록 (CSR): 용어 번호로 정렬
        order = np.argsort(terms, kind="stable")
        self.doc_ids = dids[order]
        self.weights = weights[order].astype(np.float32)
        self.ptr = np.zeros(len(vocab) + 1, dtype=np.int64)
        np.cumsum(df.astype(np.int64), out=self.ptr[1:])

    def score(self, tokens: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """(BM25 점수, coverage) — 둘 다 문서 수 길이 배열."""
        scores = np.zeros(self.n, dtype=np.float32)
        covered = np.zeros(self.n, dtype=np.float32)
        total_idf = 0.0
        for t in tokens:
            j = self.vocab.get(t)
            if j is None:
                total_idf += math.log(1 + (self.n + 0.5) / 0.5)  # 처음 보는 단어도 분모에 넣는다
                continue
            s, e = self.ptr[j], self.ptr[j + 1]
            ids = self.doc_ids[s:e]
            scores[ids] += self.weights[s:e]
            covered[ids] += self.idf[j]
            total_idf += float(self.idf[j])
        coverage = covered / total_idf if total_idf > 0 else covered
        return scores, coverage


class Retriever:
    """청크 목록을 받아 질문과 관련된 근거를 찾는다."""

    def __init__(
        self,
        chunks: list[Chunk],
        vectors: np.ndarray | None = None,
        embedder=None,
    ) -> None:
        self.chunks = chunks
        self.bm25 = BM25Index([index_text(c) for c in chunks])
        self.vectors = vectors
        self.embedder = embedder
        self._gas_tags = [chunk_gas_tags(c) for c in chunks]
        self._src = np.array([c.source_type for c in chunks], dtype=object)
        self._prio = np.array([PRIORITY_WEIGHT.get(c.priority, 0.75) for c in chunks], dtype=np.float32)
        self.lexicon = build_lexicon(list({c.doc_key: doc_title(c) for c in chunks}.values()))
        self.vector_error: str | None = None  # 마지막 벡터 검색 실패 사유 (관리자 확인용)

    @property
    def uses_vectors(self) -> bool:
        return self.vectors is not None and self.embedder is not None

    @classmethod
    def from_directory(cls, chunks_dir: Path, index_dir: Path | None = None, embedder=None) -> Retriever:
        from ingest.chunker import load_chunks

        chunks = load_chunks(chunks_dir)
        vectors = None
        if index_dir is not None and embedder is not None:
            vectors = load_vectors(index_dir, chunks, embedder.model_name)
        return cls(chunks, vectors=vectors, embedder=embedder)

    def _mask(self, filters: SearchFilters | None) -> np.ndarray:
        mask = np.ones(len(self.chunks), dtype=bool)
        if filters and filters.gas_categories:
            wanted = set(filters.gas_categories) | {"공통"}
            mask &= np.fromiter((bool(tags & wanted) for tags in self._gas_tags), dtype=bool, count=len(self.chunks))
        if filters and filters.source_types:
            mask &= np.isin(self._src, list(filters.source_types))
        return mask

    def search(self, query: str, filters: SearchFilters | None = None, top_k: int = 6) -> list[RetrievedChunk]:
        """질문과 관련된 근거를 점수 내림차순으로 반환한다."""
        if not self.chunks or not query.strip():
            return []
        mask = self._mask(filters)
        bm25, _ = self.bm25.score(query_tokens(query))
        dom = domain_tokens(query, self.lexicon)
        if dom:
            _, coverage = self.bm25.score(dom)
        else:
            coverage = np.zeros(len(self.chunks), dtype=np.float32)  # 분야 용어가 없는 질문
        bm25 = np.where(mask, bm25, -1.0)

        ranked_lists = [np.argsort(-bm25)[:CANDIDATES]]
        ranked_lists[0] = ranked_lists[0][bm25[ranked_lists[0]] > 0]
        if self.uses_vectors:
            try:
                qv = self.embedder.embed_query(query)
                sims = self.vectors @ qv
                ranked = np.argsort(-np.where(mask, sims, -np.inf))[:CANDIDATES]
                ranked_lists.append(ranked[mask[ranked]])  # 필터 밖 후보 제거 (REVIEW M5)
                self.vector_error = None
            except Exception as e:  # 임베딩 API 장애 → 키워드 검색만으로 계속 (REVIEW M8)
                self.vector_error = f"{type(e).__name__}: {e}"[:200]

        fused: dict[int, float] = {}
        for ranked in ranked_lists:
            for rank, idx in enumerate(ranked):
                fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (RRF_K + rank + 1)

        hinted = gas_hints(query)

        def final_score(i: int) -> float:
            s = fused[i] * float(self._prio[i])
            if hinted and self._gas_tags[i] & hinted:
                s *= GAS_BOOST
            return s

        order = sorted(fused, key=final_score, reverse=True)
        results: list[RetrievedChunk] = []
        per_doc: Counter = Counter()
        for i in order:
            ch = self.chunks[i]
            if per_doc[ch.doc_key] >= MAX_CHUNKS_PER_DOC:
                continue
            per_doc[ch.doc_key] += 1
            results.append(RetrievedChunk(chunk=ch, score=final_score(i), coverage=float(coverage[i])))
            if len(results) >= top_k:
                break
        return results


# ── 벡터 저장 ──────────────────────────────────────────────


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def load_vectors(index_dir: Path, chunks: list[Chunk], model_name: str) -> np.ndarray | None:
    """embeddings.npz에서 청크 순서대로 벡터를 꺼낸다. 하나라도 없으면 None (BM25만 사용)."""
    path = index_dir / "embeddings.npz"
    if not path.exists():
        return None
    data = np.load(path, allow_pickle=False)
    if str(data["model"]) != model_name:
        return None
    lookup = {h: i for i, h in enumerate(data["hashes"].tolist())}
    rows = [lookup.get(_text_hash(c.text)) for c in chunks]
    if any(r is None for r in rows):
        return None
    return data["vectors"][rows].astype(np.float32)


def build_vectors(index_dir: Path, chunks: list[Chunk], embedder, log=print) -> int:
    """바뀐 청크만 새로 임베딩해 embeddings.npz를 갱신한다. 새로 임베딩한 개수를 반환."""
    path = index_dir / "embeddings.npz"
    old: dict[str, np.ndarray] = {}
    if path.exists():
        data = np.load(path, allow_pickle=False)
        if str(data["model"]) == embedder.model_name:
            old = dict(zip(data["hashes"].tolist(), data["vectors"]))
    hashes = [_text_hash(c.text) for c in chunks]
    todo = [i for i, h in enumerate(hashes) if h not in old]
    if todo:
        log(f"  임베딩 {len(todo)}개 생성 중 ({embedder.model_name})")
        new = embedder.embed_documents([chunks[i].text for i in todo])
        for i, v in zip(todo, new):
            old[hashes[i]] = v
    uniq = list(dict.fromkeys(hashes))
    index_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        hashes=np.array(uniq),
        vectors=np.stack([old[h] for h in uniq]).astype(np.float16),
        model=np.array(embedder.model_name),
    )
    return len(todo)
