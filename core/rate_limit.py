"""사용량 제한 (F-16)과 관리자 로그인 시도 제한. 전체 공개 앱의 비용·보안 보호용.

- 사용자별 하루 질문 수: 브라우저 세션이 아니라 접속 IP 해시로 센다 → 새로고침·새 탭으로 초기화되지 않음.
  IP는 X-Forwarded-For의 **오른쪽**(신뢰하는 프록시가 붙인 값)에서 고른다. 맨 앞 값은 클라이언트가
  마음대로 넣을 수 있어 한도 우회에 쓰인다 (REVIEW M1). IP를 알 수 없으면 세션 ID로 센다.
  IP와 그 해시는 메모리에만 두고 로그에 저장하지 않는다.
- 앱 전체 하루 질문 수: 비용 상한.
- 차감은 LLM 호출 **전에** 원자적으로 예약하고(reserve), 오류로 답을 못 만들면 환급한다(refund).
  확인과 차감 사이 틈으로 동시 요청이 한도를 넘지 못한다.
- 하루 경계는 한국 시간(KST) 자정이다.
- 카운터는 서버 프로세스 메모리에 있으므로 앱이 재시작되면 초기화된다.
  (여러 인스턴스·재시작에도 유지하려면 Supabase 등 외부 저장소로 옮겨야 함)
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time
from dataclasses import dataclass

from core.config import get_setting, today_kst


@dataclass
class LimitResult:
    allowed: bool
    remaining: int
    message: str = ""


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def pick_client_ip(forwarded_for: str | None, direct_ip: str | None, trusted_hops: int) -> str | None:
    """클라이언트 IP를 고른다.

    X-Forwarded-For는 "클라이언트가 보낸 값, ..., 프록시가 본 실제 IP" 순으로 오른쪽에 덧붙는다.
    신뢰하는 프록시가 trusted_hops개면 오른쪽에서 trusted_hops번째가 프록시가 확인한 클라이언트 IP다.
    그보다 왼쪽 값은 클라이언트가 위조할 수 있으므로 쓰지 않는다.
    """
    if forwarded_for and trusted_hops > 0:
        parts = [p for p in (x.strip() for x in forwarded_for.split(",")) if p]
        if len(parts) >= trusted_hops:
            ip = _valid_ip(parts[-trusted_hops])
            if ip:
                return ip
    return _valid_ip(direct_ip) if direct_ip else None


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day = today_kst()
        self._per_client: dict[str, int] = {}
        self._total = 0
        self._salt = secrets.token_hex(16)  # 프로세스마다 다른 값 → 해시를 다른 곳과 대조할 수 없음

    def _roll(self) -> None:
        today = today_kst()
        if today != self._day:
            self._day = today
            self._per_client.clear()
            self._total = 0

    def client_key(self, ip: str | None, session_id: str) -> str:
        raw = ip or f"session:{session_id}"
        return hashlib.sha256(f"{self._salt}:{raw}".encode()).hexdigest()[:24]

    def _remaining_locked(self, key: str) -> int:
        per = get_setting("MAX_QUESTIONS_PER_CLIENT_PER_DAY") - self._per_client.get(key, 0)
        total = get_setting("MAX_QUESTIONS_PER_DAY") - self._total
        return max(min(per, total), 0)

    def remaining(self, key: str) -> int:
        with self._lock:
            self._roll()
            return self._remaining_locked(key)

    def check(self, key: str, question: str) -> LimitResult:
        """질문을 받아도 되는지 미리 판단한다 (횟수를 올리지는 않음). 실제 차감은 reserve()."""
        max_len = get_setting("MAX_QUESTION_LENGTH")
        if not question.strip():
            return LimitResult(False, self.remaining(key), "질문을 입력해 주세요.")
        if len(question) > max_len:
            return LimitResult(
                False, self.remaining(key), f"질문은 {max_len}자 이내로 입력해 주세요. (현재 {len(question)}자)"
            )
        return self._limit_result(key)

    def _limit_result(self, key: str) -> LimitResult:
        with self._lock:
            self._roll()
            if self._total >= get_setting("MAX_QUESTIONS_PER_DAY"):
                return LimitResult(False, 0, "오늘 서비스 전체 질문 한도에 도달했습니다. 내일 다시 이용해 주세요.")
            remaining = self._remaining_locked(key)
        if remaining <= 0:
            limit = get_setting("MAX_QUESTIONS_PER_CLIENT_PER_DAY")
            return LimitResult(False, 0, f"오늘 질문 가능 횟수({limit}회)를 모두 사용했습니다. 내일 다시 이용해 주세요.")
        return LimitResult(True, remaining)

    def reserve(self, key: str) -> LimitResult:
        """LLM 호출 1회분을 원자적으로 확인·차감한다. 허용되면 remaining은 차감 후 값."""
        with self._lock:
            self._roll()
            if self._total >= get_setting("MAX_QUESTIONS_PER_DAY"):
                return LimitResult(False, 0, "오늘 서비스 전체 질문 한도에 도달했습니다. 내일 다시 이용해 주세요.")
            if self._remaining_locked(key) <= 0:
                limit = get_setting("MAX_QUESTIONS_PER_CLIENT_PER_DAY")
                return LimitResult(False, 0, f"오늘 질문 가능 횟수({limit}회)를 모두 사용했습니다. 내일 다시 이용해 주세요.")
            self._per_client[key] = self._per_client.get(key, 0) + 1
            self._total += 1
            return LimitResult(True, self._remaining_locked(key))

    def refund(self, key: str) -> None:
        """예약했지만 오류로 답변을 못 만든 1회분을 돌려준다."""
        with self._lock:
            if self._per_client.get(key, 0) > 0:
                self._per_client[key] -= 1
                self._total = max(self._total - 1, 0)

    def record(self, key: str) -> None:
        """(호환용) 예약 없이 1회 기록한다."""
        with self._lock:
            self._roll()
            self._per_client[key] = self._per_client.get(key, 0) + 1
            self._total += 1


class LoginThrottle:
    """관리자 로그인 시도 제한 (REVIEW M9). 키(IP 해시 또는 세션)별로 연속 실패를 세고 잠근다."""

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._clock = clock

    def locked_for(self, key: str) -> int:
        """남은 잠금 시간(초). 0이면 시도 가능."""
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            left = until - self._clock()
            if left <= 0 and key in self._locked_until:
                del self._locked_until[key]
                self._fails.pop(key, None)
            return max(int(left + 0.999), 0)

    def failure(self, key: str) -> int:
        """실패 1회 기록. 잠기면 잠금 시간(초)을 반환."""
        with self._lock:
            self._fails[key] = self._fails.get(key, 0) + 1
            if self._fails[key] >= get_setting("ADMIN_MAX_FAILS"):
                seconds = get_setting("ADMIN_LOCK_SECONDS")
                self._locked_until[key] = self._clock() + seconds
                return seconds
            return 0

    def success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)
