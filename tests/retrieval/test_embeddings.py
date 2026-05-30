from __future__ import annotations

from src.semantic_registry.retrieval.documents import RetrievalDoc, RetrievalDocType
from src.semantic_registry.retrieval.embeddings import EmbeddingService, search_similar, sync_embeddings


class DummyModel:
    def encode(self, texts):
        def vector(text: str) -> list[float]:
            return [1.0, 0.0, 0.0] if "orders" in text else [0.0, 1.0, 0.0]

        if isinstance(texts, list):
            return [vector(text) for text in texts]
        return vector(texts)

    def get_sentence_embedding_dimension(self) -> int:
        return 3


def test_embedding_service_lazy_loads_and_uses_model() -> None:
    service = EmbeddingService("dummy")
    service._model = DummyModel()

    assert service.dimension() == 3
    assert service.embed("orders table") == [1.0, 0.0, 0.0]
    assert service.embed_batch(["orders", "customers"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


async def test_sync_embeddings_stores_only_changed_docs(in_memory_session) -> None:
    service = EmbeddingService("dummy")
    service._model = DummyModel()
    docs = [
        RetrievalDoc(
            id="table:orders",
            doc_type=RetrievalDocType.table,
            doc_name="orders",
            content="orders table",
        )
    ]

    stored = await sync_embeddings(in_memory_session, docs, service)
    unchanged = await sync_embeddings(in_memory_session, docs, service)
    similar = await search_similar(in_memory_session, [1.0, 0.0, 0.0], doc_type="table", top_k=1)

    assert len(stored) == 1
    assert unchanged == []
    assert similar == [("orders", "table", 1.0)]
