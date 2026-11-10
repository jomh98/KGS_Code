"""임베딩 공급자 인터페이스.

임베딩 모델은 PRD §13.2에서 아직 미결이므로 공급자를 교체할 수 있게 분리한다.
EMBEDDING_API_KEY가 없으면 get_embedder()가 None을 반환하고, 검색은 BM25만으로 동작한다.

기본 구현은 Voyage AI 임베딩 API(한국어 포함 다국어 지원)다. 모델명은 EMBEDDING_MODEL
설정으로 바꾼다. ※ 키가 없어 실제 호출은 아직 확인하지 못했다.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
import requests

from core.config import get_secret, get_setting


class Embedder(Protocol):
    model_name: str

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class VoyageEmbedder:
    URL = "https://api.voyageai.com/v1/embeddings"
    BATCH = 64

    def __init__(self, api_key: str, model_name: str) -> None:
        self.api_key = api_key
        self.model_name = model_name

    def _embed(self, texts: list[str], input_type: str) -> np.ndarray:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            resp = requests.post(
                self.URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"input": texts[i : i + self.BATCH], "model": self.model_name, "input_type": input_type},
                timeout=120,
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors.extend(d["embedding"] for d in data)
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.maximum(norms, 1e-12)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "query")[0]


def get_embedder() -> Embedder | None:
    key = get_secret("EMBEDDING_API_KEY")
    if not key:
        return None
    return VoyageEmbedder(key, get_setting("EMBEDDING_MODEL"))
