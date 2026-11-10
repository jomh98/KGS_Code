import pytest

import core.logger as logger
from core.models import QueryLog
from core.rate_limit import RateLimiter


@pytest.mark.parametrize(
    "text, expected",
    [
        ("연락처 010-1234-5678로 주세요", "연락처 [전화번호]로 주세요"),
        ("01012345678", "[전화번호]"),
        ("사무실 02-123-4567", "사무실 [전화번호]"),
        ("주민번호 900101-1234567 입니다", "주민번호 [주민등록번호] 입니다"),
        ("메일 test.user@example.co.kr 로", "메일 [이메일] 로"),
        ("카드 1234-5678-9012-3456", "카드 [카드번호]"),
    ],
)
def test_mask_pii(text, expected):
    assert logger.mask_pii(text) == expected


@pytest.mark.parametrize(
    "text",
    ["압력은 0.2㎫ 이상", "KGS FS551 2.5.8.2", "1544-4500", "배관 두께 150㎜, 2026-09-11 개정"],
)
def test_mask_pii_leaves_technical_text(text):
    assert logger.mask_pii(text) == text


def test_local_log_roundtrip_with_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(logger, "LOCAL_PATH", tmp_path / "q.jsonl")
    entry = QueryLog(session_id="s1", question="제 번호는 010-9999-8888 입니다", answer="답변")
    assert logger.save_log(entry, background=False)
    assert logger.save_feedback(entry.id, "up", background=False)
    rows = logger.read_logs()
    assert len(rows) == 1
    assert rows[0]["feedback"] == "up"
    assert "010-9999-8888" not in rows[0]["question"]
    assert "ip" not in rows[0]


def test_log_failure_does_not_raise(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(logger, "_supabase", boom)
    assert logger.save_log(QueryLog(session_id="s", question="q", answer="a"), background=False) is False


def test_rate_limit_per_client(monkeypatch):
    import core.rate_limit as rl

    monkeypatch.setattr(
        rl, "get_setting",
        lambda n: {"MAX_QUESTIONS_PER_CLIENT_PER_DAY": 2, "MAX_QUESTIONS_PER_DAY": 100, "MAX_QUESTION_LENGTH": 20}[n],
    )
    lim = RateLimiter()
    a = lim.client_key("1.2.3.4", "sessA")
    b = lim.client_key("1.2.3.4", "sessB")  # 새로고침(새 세션)이어도 같은 IP면 같은 키
    assert a == b
    assert lim.check(a, "질문").allowed
    lim.record(a)
    lim.record(a)
    res = lim.check(b, "질문")
    assert not res.allowed and res.remaining == 0
    other = lim.client_key("5.6.7.8", "sessC")
    assert lim.check(other, "질문").allowed


def test_rate_limit_length_and_empty(monkeypatch):
    import core.rate_limit as rl

    monkeypatch.setattr(
        rl, "get_setting",
        lambda n: {"MAX_QUESTIONS_PER_CLIENT_PER_DAY": 5, "MAX_QUESTIONS_PER_DAY": 100, "MAX_QUESTION_LENGTH": 10}[n],
    )
    lim = RateLimiter()
    k = lim.client_key(None, "s")
    assert not lim.check(k, "   ").allowed
    assert "10자" in lim.check(k, "가" * 11).message


def test_rate_limit_global(monkeypatch):
    import core.rate_limit as rl

    monkeypatch.setattr(
        rl, "get_setting",
        lambda n: {"MAX_QUESTIONS_PER_CLIENT_PER_DAY": 5, "MAX_QUESTIONS_PER_DAY": 1, "MAX_QUESTION_LENGTH": 100}[n],
    )
    lim = RateLimiter()
    lim.record(lim.client_key("1.1.1.1", "s"))
    res = lim.check(lim.client_key("2.2.2.2", "t"), "질문")
    assert not res.allowed and "전체" in res.message
