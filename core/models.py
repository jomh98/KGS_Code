"""공통 데이터 모델 (PRD §5.3 청크 메타데이터, §5.6 질문 로그).

날짜는 JSON 직렬화를 단순하게 하기 위해 "YYYY-MM-DD" 문자열로 다룬다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

SourceType = Literal["법률", "시행령", "시행규칙", "행정규칙", "KGS"]
Status = Literal["현행", "시행예정"]
GasCategory = Literal["고압가스", "LPG", "도시가스", "수소", "공통"]
Priority = Literal["A", "B", "C"]


@dataclass
class Chunk:
    """검색 단위가 되는 법령 조문 / KGS Code 항목 조각."""

    chunk_id: str
    source_type: SourceType
    doc_name: str  # 예: "도시가스사업법 시행규칙", "KGS FS551"
    article_or_item: str  # 예: "제17조", "2.5.3"
    text: str
    url: str
    effective_date: str | None = None  # 시행일
    amended_date: str | None = None  # 제정/개정일
    status: Status = "현행"
    gas_category: GasCategory = "공통"
    priority: Priority = "A"  # config/target_laws.yaml 우선순위 (A > B > C)
    collected_at: str | None = None
    page: int | None = None  # KGS Code 본문 쪽번호

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chunk:
        return cls(**data)

    @property
    def citation(self) -> str:
        """답변 근거 목록에 표시할 한 줄 표기."""
        date_label = "시행" if self.source_type != "KGS" else "개정"
        date = self.effective_date if self.source_type != "KGS" else self.amended_date
        suffix = f" ({date_label} {date})" if date else ""
        if self.status == "시행예정":
            suffix += " [시행예정]"
        return f"{self.doc_name} {self.article_or_item}{suffix}"

    @property
    def doc_key(self) -> str:
        """같은 문서(법령·코드)에 속한 청크를 묶는 키."""
        return f"{self.source_type}:{self.doc_name}"


@dataclass
class RetrievedChunk:
    """검색 결과: 청크 + 점수.

    score: 최종 순위 점수(하이브리드). coverage: 질문 키워드가 이 청크에 얼마나 들어 있는지(0~1),
    근거 부족 판단(F-06)에 쓴다.
    """

    chunk: Chunk
    score: float
    coverage: float = 0.0


@dataclass
class Answer:
    """LLM 답변과 인용 근거."""

    text: str
    # 검색된 근거 전체. 답변의 [근거 n]은 이 목록의 n번째(1부터)를 가리킨다.
    sources: list[RetrievedChunk] = field(default_factory=list)
    cited: list[int] = field(default_factory=list)  # 답변에 실제 인용된 근거 번호
    insufficient_evidence: bool = False  # F-06 근거 부족 여부
    latency_ms: int = 0
    model: str = ""
    # 상태 (REVIEW C2: 오류 응답이 캐시·차감되지 않도록 명시적으로 구분)
    error: bool = False  # API 오류 등으로 답변을 만들지 못함 → 캐시 안 함, 사용량 환급
    llm_called: bool = False  # 실제로 LLM 답변을 받았음 (사용량 차감 대상)
    cacheable: bool = False  # 정상 완료된 답변만 True (잘림·거절·오류는 False)
    removed_citations: list[int] = field(default_factory=list)  # 존재하지 않아 지운 [근거 n] 번호


@dataclass
class QueryLog:
    """질문 로그 1건 (PRD §5.6). IP·개인 식별정보는 저장하지 않는다."""

    session_id: str
    question: str
    answer: str
    cited_sources: list[str] = field(default_factory=list)  # chunk_id 목록
    filters: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""
    feedback: str | None = None  # "up" / "down" / 자유 의견
    data_version: str | None = None  # 색인 기준일
    insufficient_evidence: bool = False
    error: bool = False
    removed_citations: list[int] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
