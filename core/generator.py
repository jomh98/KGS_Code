"""답변 생성 (F-03, F-06, PRD §5.4).

흐름:
1. 근거가 없거나 최상위 근거의 coverage가 임계값 미만 → LLM을 호출하지 않고 근거 부족 안내
2. ANTHROPIC_API_KEY가 없으면 → LLM 없이 검색된 근거 목록만 보여준다
3. Claude 스트리밍 호출 → 끝난 뒤 [근거 n] 번호를 검증해 존재하지 않는 번호를 제거

답변 상태(Answer.error / llm_called / cacheable)로 호출 측이 캐시·사용량 차감 여부를 정한다.
오류 응답은 절대 캐시하지 않는다 (REVIEW C2).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator

from core.config import get_secret, get_setting
from core.models import Answer, RetrievedChunk
from core.prompts import SYSTEM_PROMPT, build_user_message

log = logging.getLogger(__name__)

INSUFFICIENT_MARK = "확인된 근거 없음"
INSUFFICIENT_MESSAGE = (
    f"**{INSUFFICIENT_MARK}**\n\n현재 수집된 법령·KGS Code에서 이 질문에 답할 근거를 찾지 못했습니다. "
    "질문을 더 구체적으로 바꿔 보시거나(시설 종류, 가스 종류 등), 한국가스안전공사(국번 없이 1544-4500) 등 "
    "관할 기관에 문의해 주세요."
)
NO_LLM_MESSAGE = (
    "답변 생성용 API 키가 설정되지 않아 요약 답변 없이 **검색된 근거만** 보여 드립니다. "
    "아래 근거 원문을 확인해 주세요."
)
ERROR_AUTH = "답변 생성 API 키가 올바르지 않습니다. 관리자에게 문의해 주세요. 아래 근거는 확인하실 수 있습니다."
ERROR_BUSY = "지금 요청이 많아 답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요. 아래 근거는 확인하실 수 있습니다."
ERROR_CONN = "답변 생성 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요. 아래 근거는 확인하실 수 있습니다."
ERROR_UNKNOWN = "답변을 만드는 중 문제가 생겼습니다. 잠시 후 다시 시도해 주세요. 아래 근거는 확인하실 수 있습니다."
REFUSAL_MESSAGE = "이 질문에는 답변을 생성할 수 없습니다. 아래 근거 원문을 직접 확인해 주세요."

_CITE_GROUP = re.compile(r"\[근거\s*([\d,\s]+)\]")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def validate_citations(text: str, num_sources: int) -> tuple[str, list[int], list[int]]:
    """존재하지 않는 [근거 n]을 제거한다.

    반환: (정리된 텍스트, 실제 인용된 번호 목록(등장 순), 제거한 번호 목록)
    "[근거 1, 3]" 형식도 처리한다.
    """
    cited: list[int] = []
    removed: list[int] = []

    def repl(m: re.Match) -> str:
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        keep = [n for n in nums if 1 <= n <= num_sources]
        removed.extend(n for n in nums if n not in keep)
        for n in keep:
            if n not in cited:
                cited.append(n)
        return "".join(f"[근거 {n}]" for n in keep)

    cleaned = _CITE_GROUP.sub(repl, text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned, cited, removed


def is_insufficient(sources: list[RetrievedChunk]) -> bool:
    if not sources:
        return True
    return max(s.coverage for s in sources) < get_setting("MIN_RETRIEVAL_SCORE")


def will_call_llm(sources: list[RetrievedChunk]) -> bool:
    """이 근거로 답변을 만들면 LLM을 호출하게 되는지 (사용량을 미리 예약할지 판단용)."""
    return not is_insufficient(sources) and bool(get_secret("ANTHROPIC_API_KEY"))


def says_insufficient(text: str) -> bool:
    """LLM 답변이 근거 부족을 선언했는지. "**답변:** 확인된 근거 없음…" 형식도 잡는다."""
    return INSUFFICIENT_MARK in text[:200]


class AnswerStream:
    """st.write_stream에 넘길 수 있는 스트림. 다 읽은 뒤 .answer로 검증된 최종 답변을 얻는다."""

    def __init__(self, question: str, sources: list[RetrievedChunk]) -> None:
        self.question = question
        self.sources = sources
        self.answer: Answer | None = None
        self._start = time.perf_counter()

    def _finish(
        self,
        text: str,
        model: str,
        *,
        insufficient: bool = False,
        used_sources: list[RetrievedChunk] | None = None,
        cited: list[int] | None = None,
        error: bool = False,
        llm_called: bool = False,
        cacheable: bool = False,
        removed: list[int] | None = None,
    ) -> None:
        self.answer = Answer(
            text=text,
            sources=used_sources if used_sources is not None else self.sources,
            cited=cited or [],
            insufficient_evidence=insufficient,
            latency_ms=int((time.perf_counter() - self._start) * 1000),
            model=model,
            error=error,
            llm_called=llm_called,
            cacheable=cacheable,
            removed_citations=removed or [],
        )

    def __iter__(self) -> Iterator[str]:
        if is_insufficient(self.sources):
            self._finish(INSUFFICIENT_MESSAGE, model="", insufficient=True, used_sources=[])
            yield INSUFFICIENT_MESSAGE
            return

        api_key = get_secret("ANTHROPIC_API_KEY")
        if not api_key:
            self._finish(NO_LLM_MESSAGE, model="none")
            yield NO_LLM_MESSAGE
            return

        yield from self._stream_claude(api_key)

    def _fail(self, msg: str, model: str) -> Iterator[str]:
        self._finish(msg, model=model, error=True)
        yield msg

    def _stream_claude(self, api_key: str) -> Iterator[str]:
        import anthropic

        model = get_setting("LLM_MODEL")
        parts: list[str] = []
        stop_reason = None
        try:
            client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)
            with client.beta.messages.stream(
                model=model,
                max_tokens=get_setting("MAX_ANSWER_TOKENS"),
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": build_user_message(self.question, self.sources)}],
                output_config={"effort": get_setting("LLM_EFFORT")},
                # 안전 분류기가 거절하면 서버가 다른 모델로 다시 시도한다
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                for text in stream.text_stream:
                    parts.append(text)
                    yield text
                final = stream.get_final_message()
                stop_reason = final.stop_reason
                model = final.model or model
        except anthropic.AuthenticationError:
            yield from self._fail(ERROR_AUTH, model)
            return
        except anthropic.RateLimitError:
            yield from self._fail(ERROR_BUSY, model)
            return
        except (anthropic.APIStatusError, anthropic.APIConnectionError):
            yield from self._fail(ERROR_CONN, model)
            return
        except Exception:  # SDK 밖 예외도 화면에 traceback이 뜨지 않게 (REVIEW M8)
            log.exception("답변 생성 중 예기치 않은 오류")
            yield from self._fail(ERROR_UNKNOWN, model)
            return

        raw = "".join(parts)
        complete = stop_reason in ("end_turn", "stop_sequence")
        if stop_reason == "refusal":
            raw = REFUSAL_MESSAGE
        elif stop_reason == "max_tokens":
            raw += "\n\n_(답변이 길어 일부가 잘렸습니다. 질문을 나눠서 다시 물어봐 주세요.)_"

        cleaned, cited, removed = validate_citations(raw, len(self.sources))
        if removed:
            log.warning("존재하지 않는 근거 번호 제거: %s", removed)
        self._finish(
            cleaned,
            model=model,
            insufficient=says_insufficient(cleaned),
            cited=cited,
            llm_called=True,
            cacheable=complete,
            removed=removed,
        )


def generate(question: str, sources: list[RetrievedChunk]) -> Answer:
    """스트리밍 없이 최종 답변만 필요할 때."""
    stream = AnswerStream(question, sources)
    for _ in stream:
        pass
    assert stream.answer is not None
    return stream.answer
