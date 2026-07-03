from __future__ import annotations

import hashlib
import math
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, JSON, String, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from src.semantic_registry.models.base import Base, GUID
from src.semantic_registry.retrieval.documents import RetrievalDoc, RetrievalDocType

try:  # pragma: no cover - exercised when pgvector is installed.
    from pgvector.sqlalchemy import Vector as PgVector
except Exception:  # pragma: no cover - deterministic fallback for lightweight test envs.
    PgVector = None  # type: ignore[assignment]


SCHEMA_NAME = "semantic"
DEFAULT_EMBEDDING_DIMENSION = 384


class JsonVector(TypeDecorator[list[float]]):
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, _dialect: Any) -> list[float] | None:
        if value is None:
            return None
        return [float(item) for item in value]

    def process_result_value(self, value: Any, _dialect: Any) -> list[float] | None:
        if value is None:
            return None
        return [float(item) for item in value]


def vector_type(dimension: int = DEFAULT_EMBEDDING_DIMENSION) -> Any:
    if PgVector is None:
        return JsonVector()
    return PgVector(dimension).with_variant(JsonVector(), "sqlite")


class RetrievalEmbedding(Base):
    __tablename__ = "retrieval_embeddings"
    __table_args__ = (
        UniqueConstraint("doc_type", "doc_name", name="uq_retrieval_embeddings_doc"),
        {"schema": SCHEMA_NAME},
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    doc_type: Mapped[RetrievalDocType] = mapped_column(
        Enum(RetrievalDocType, native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    doc_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding: Mapped[list[float]] = mapped_column(vector_type(DEFAULT_EMBEDDING_DIMENSION), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class EmbeddingService:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self.model_name = model_name
        self._model: Any | None = None

    @property
    def model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, text: str) -> list[float]:
        vector = self.model.encode(text)
        if hasattr(vector, "tolist"):
            return [float(item) for item in vector.tolist()]
        return [float(item) for item in vector]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts)
        if hasattr(vectors, "tolist"):
            vectors = vectors.tolist()
        return [[float(item) for item in vector] for vector in vectors]

    def dimension(self) -> int:
        if self._model is not None and hasattr(self._model, "get_sentence_embedding_dimension"):
            dimension = self._model.get_sentence_embedding_dimension()
            if dimension:
                return int(dimension)
        return DEFAULT_EMBEDDING_DIMENSION


async def _get_existing(session: AsyncSession, doc: RetrievalDoc) -> RetrievalEmbedding | None:
    result = await session.execute(
        select(RetrievalEmbedding).where(
            RetrievalEmbedding.doc_type == doc.doc_type,
            RetrievalEmbedding.doc_name == doc.doc_name,
        )
    )
    return result.scalar_one_or_none()


async def store_embedding(session: AsyncSession, doc: RetrievalDoc) -> RetrievalEmbedding:
    if doc.embedding is None:
        raise ValueError("RetrievalDoc.embedding is required before storing")
    existing = await _get_existing(session, doc)
    if existing is None:
        existing = RetrievalEmbedding(
            doc_type=doc.doc_type,
            doc_name=doc.doc_name,
            content_hash=doc.content_hash,
            embedding=doc.embedding,
        )
        session.add(existing)
    else:
        existing.content_hash = doc.content_hash
        existing.embedding = doc.embedding
    await session.commit()
    await session.refresh(existing)
    return existing


async def batch_store_embeddings(session: AsyncSession, docs: list[RetrievalDoc]) -> list[RetrievalEmbedding]:
    stored: list[RetrievalEmbedding] = []
    for doc in docs:
        if doc.embedding is None:
            raise ValueError(f"RetrievalDoc.embedding is required before storing {doc.doc_name}")
        existing = await _get_existing(session, doc)
        if existing is None:
            existing = RetrievalEmbedding(
                doc_type=doc.doc_type,
                doc_name=doc.doc_name,
                content_hash=doc.content_hash,
                embedding=doc.embedding,
            )
            session.add(existing)
        else:
            existing.content_hash = doc.content_hash
            existing.embedding = doc.embedding
        stored.append(existing)
    await session.commit()
    for row in stored:
        await session.refresh(row)
    return stored


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    left = left[:size]
    right = right[:size]
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, min(1.0, numerator / (left_norm * right_norm)))


async def search_similar(
    session: AsyncSession,
    query_embedding: list[float],
    doc_type: str | None = None,
    top_k: int = 10,
) -> list[tuple[str, str, float]]:
    dialect_name = session.get_bind().dialect.name
    if PgVector is not None and dialect_name == "postgresql":
        distance = RetrievalEmbedding.embedding.cosine_distance(query_embedding)
        stmt = select(RetrievalEmbedding.doc_name, RetrievalEmbedding.doc_type, (1 - distance).label("score"))
        if doc_type is not None:
            stmt = stmt.where(RetrievalEmbedding.doc_type == doc_type)
        rows = (await session.execute(stmt.order_by(distance).limit(top_k))).all()
        return [(str(name), str(kind.value if hasattr(kind, "value") else kind), float(score)) for name, kind, score in rows]

    stmt = select(RetrievalEmbedding)
    if doc_type is not None:
        stmt = stmt.where(RetrievalEmbedding.doc_type == doc_type)
    rows = (await session.execute(stmt)).scalars().all()
    scored = [
        (
            row.doc_name,
            str(row.doc_type.value if hasattr(row.doc_type, "value") else row.doc_type),
            _cosine_similarity(query_embedding, row.embedding),
        )
        for row in rows
    ]
    return sorted(scored, key=lambda item: item[2], reverse=True)[:top_k]


async def sync_embeddings(
    session: AsyncSession,
    docs: list[RetrievalDoc],
    embedding_service: EmbeddingService,
) -> list[RetrievalEmbedding]:
    changed_docs: list[RetrievalDoc] = []
    for doc in docs:
        existing = await _get_existing(session, doc)
        if existing is None or existing.content_hash != doc.content_hash:
            changed_docs.append(doc)
    if not changed_docs:
        return []
    embeddings = embedding_service.embed_batch([doc.content for doc in changed_docs])
    for doc, embedding in zip(changed_docs, embeddings):
        doc.embedding = embedding
    return await batch_store_embeddings(session, changed_docs)


__all__ = [
    "EmbeddingService",
    "RetrievalEmbedding",
    "batch_store_embeddings",
    "content_hash",
    "search_similar",
    "store_embedding",
    "sync_embeddings",
    "vector_type",
]
