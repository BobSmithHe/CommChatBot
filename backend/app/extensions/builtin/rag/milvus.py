from __future__ import annotations

import time
from typing import Any

from app.infra.config import get_settings
from app.extensions.support.embeddings import get_embedding_model


class MilvusRagIndex:
    """Lazy Milvus mirror with a local-RAG fallback when the service is offline."""

    collection_name = "commchat_knowledge"

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None
        self._model = None
        self._retry_after = 0.0

    def upsert(self, chunks: list[dict[str, Any]]) -> bool:
        if not chunks or not self._ensure():
            return False
        texts = [str(item["content"]) for item in chunks]
        vectors = self._model.encode(texts, normalize_embeddings=True).tolist()
        rows = []
        for item, vector in zip(chunks, vectors):
            rows.append({
                "id": str(item["chunk_id"]),
                "vector": vector,
                "doc_id": str(item["doc_id"]),
                "source": str(item["source"]),
                "title": str(item.get("title") or ""),
                "content": str(item["content"]),
            })
        self._client.upsert(collection_name=self.collection_name, data=rows, timeout=10)
        return True

    def delete_document(self, doc_id: str) -> bool:
        if not self._ensure():
            return False
        escaped = doc_id.replace('"', '\\"')
        self._client.delete(collection_name=self.collection_name, filter=f'doc_id == "{escaped}"', timeout=10)
        return True

    def clear(self) -> bool:
        if not self._ensure():
            return False
        if self._client.has_collection(collection_name=self.collection_name):
            self._client.drop_collection(collection_name=self.collection_name)
        self._client = None
        self._retry_after = 0.0
        return True

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        if not query or not self._ensure():
            return []
        vector = self._model.encode(query, normalize_embeddings=True).tolist()
        results = self._client.search(
            collection_name=self.collection_name,
            data=[vector],
            limit=limit,
            output_fields=["doc_id", "source", "title", "content"],
            search_params={"metric_type": "COSINE", "params": {}},
            timeout=10,
        )
        rows = []
        for hit in (results[0] if results else []):
            entity = hit.get("entity") or {}
            rows.append({
                "chunk_id": str(hit.get("id") or ""),
                "doc_id": str(entity.get("doc_id") or ""),
                "source": str(entity.get("source") or ""),
                "title": str(entity.get("title") or ""),
                "content": str(entity.get("content") or ""),
                "score": float(hit.get("distance") or 0.0),
            })
        return rows

    def _ensure(self) -> bool:
        if self._client is False or time.monotonic() < self._retry_after:
            return False
        if self._client is not None:
            return True
        try:
            from pymilvus import MilvusClient

            client = MilvusClient(
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token or None,
                db_name=self.settings.milvus_db_name or "default",
                timeout=2,
            )
            if not client.has_collection(collection_name=self.collection_name):
                client.create_collection(
                    collection_name=self.collection_name,
                    dimension=self.settings.embedding_dimension,
                    primary_field_name="id",
                    id_type="string",
                    vector_field_name="vector",
                    metric_type="COSINE",
                    auto_id=False,
                    max_length=128,
                    enable_dynamic_field=True,
                )
            self._model = get_embedding_model(
                self.settings.embedding_model,
                self.settings.embedding_device,
            )
            self._client = client
            return True
        except Exception:
            self._client = None
            self._retry_after = time.monotonic() + 30
            return False
