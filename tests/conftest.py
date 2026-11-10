from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from core.models import Chunk  # noqa: E402


@pytest.fixture(autouse=True)
def no_secrets(monkeypatch):
    """테스트는 실제 키·외부 서비스를 쓰지 않는다."""
    import core.config as config

    monkeypatch.setattr(config, "_from_streamlit", lambda section, key: None)
    for env in ("ANTHROPIC_API_KEY", "LAW_API_OC", "EMBEDDING_API_KEY", "ADMIN_PASSWORD", "SUPABASE_URL", "SUPABASE_KEY"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def sample_chunks() -> list[Chunk]:
    data = json.loads((FIXTURES / "sample_chunks.json").read_text(encoding="utf-8"))
    return [Chunk.from_dict(c) for c in data["chunks"]]


@pytest.fixture
def fixture_bytes():
    return lambda name: (FIXTURES / name).read_bytes()
