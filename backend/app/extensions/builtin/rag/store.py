from __future__ import annotations

import json
import asyncio
import math
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from app.infra.config import get_settings
from .milvus import MilvusRagIndex


TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass
class RetrievedChunk:
    content: str
    score: float
    source: str
    title: str = ""
    chunk_id: str = ""


@dataclass
class KnowledgeChunk:
    chunk_id: str
    doc_id: str
    source: str
    content: str
    title: str = ""


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > chunk_size:
            chunks.append(paragraph[:chunk_size])
            paragraph = paragraph[max(0, chunk_size - overlap):]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


class LocalRagStore:
    """Local hybrid RAG store: chunking + BM25 + optional dense RRF merge."""

    def __init__(self, index_path: str | None = None) -> None:
        settings = get_settings()
        self.index_path = Path(index_path or settings.knowledge_index_path)
        self.chunk_size = settings.rag_chunk_size
        self.chunk_overlap = settings.rag_chunk_overlap
        self._chunks: list[KnowledgeChunk] = []
        self._dense_model = None
        self._dense_model_name = settings.rag_dense_model.strip()
        self._dense_vectors: list[list[float]] | None = None
        self._min_dense_score = settings.rag_min_dense_score
        self._min_lexical_score = settings.rag_min_lexical_score
        self._loaded = False
        # Explicit alternate indexes (especially tests) must never mirror into
        # the application's shared production Milvus collection.
        default_index = Path(settings.knowledge_index_path).resolve()
        self._milvus = MilvusRagIndex() if self.index_path.resolve() == default_index else None

    def _load(self) -> None:
        if self._loaded:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        if self.index_path.exists():
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._chunks = [KnowledgeChunk(**item) for item in raw.get("chunks", [])]
        self._loaded = True

    def _save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"chunks": [asdict(chunk) for chunk in self._chunks]}
        self.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._dense_vectors = None

    def add_document(self, text: str, source: str, title: str = "") -> dict:
        self._load()
        doc_id = uuid.uuid4().hex
        chunks = split_text(text, self.chunk_size, self.chunk_overlap)
        for index, content in enumerate(chunks):
            self._chunks.append(
                KnowledgeChunk(
                    chunk_id=f"{doc_id}-{index}",
                    doc_id=doc_id,
                    source=source,
                    title=title,
                    content=content,
                )
            )
        self._save()
        if self._milvus:
            self._milvus.upsert([asdict(chunk) for chunk in self._chunks if chunk.doc_id == doc_id])
        return {"doc_id": doc_id, "source": source, "chunks": len(chunks)}

    def list_documents(self) -> list[dict]:
        self._load()
        grouped: dict[str, dict] = {}
        for chunk in self._chunks:
            item = grouped.setdefault(
                chunk.doc_id,
                {"doc_id": chunk.doc_id, "source": chunk.source, "title": chunk.title, "chunks": 0},
            )
            item["chunks"] += 1
        return sorted(grouped.values(), key=lambda item: (item["source"], item["doc_id"]))

    def delete_document(self, doc_id: str) -> bool:
        self._load()
        before = len(self._chunks)
        self._chunks = [chunk for chunk in self._chunks if chunk.doc_id != doc_id]
        if len(self._chunks) == before:
            return False
        self._save()
        if self._milvus:
            self._milvus.delete_document(doc_id)
        return True

    def clear(self) -> None:
        self._chunks = []
        self._loaded = True
        self._save()
        if self._milvus:
            self._milvus.clear()

    async def search(self, query: str, top_k: int = 5) -> list[RetrievedChunk]:
        self._load()
        limit = max(top_k * 8, 20)
        bm25 = self._bm25(query, limit=limit) if self._chunks else []
        dense = await self._dense(query, limit=limit) if self._chunks else []
        milvus_rows = await asyncio.to_thread(self._milvus.search, query, limit) if self._milvus else []
        milvus = [
            RetrievedChunk(
                item["content"], item["score"], item["source"], item["title"], item["chunk_id"]
            )
            for item in milvus_rows
            if float(item.get("score") or 0.0) >= self._min_dense_score
        ]
        ranked = [items for items in (bm25, dense, milvus) if items]
        merged = self._rrf_merge(ranked)
        return merged[:top_k]

    def _bm25(self, query: str, limit: int) -> list[RetrievedChunk]:
        query_terms = tokenize(query)
        if not query_terms:
            return []

        docs = [tokenize(chunk.content) for chunk in self._chunks]
        avgdl = sum(len(doc) for doc in docs) / max(len(docs), 1)
        df = Counter()
        for doc in docs:
            for term in set(doc):
                df[term] += 1

        n_docs = len(docs)
        scored: list[RetrievedChunk] = []
        for chunk, doc_terms in zip(self._chunks, docs):
            if not doc_terms:
                continue
            tf = Counter(doc_terms)
            score = 0.0
            for term in query_terms:
                if term not in tf:
                    continue
                idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
                denom = tf[term] + 1.5 * (1 - 0.75 + 0.75 * len(doc_terms) / max(avgdl, 1))
                score += idf * (tf[term] * 2.5) / denom
            normalized = 1.0 - math.exp(-score)
            if normalized >= self._min_lexical_score:
                scored.append(self._to_result(chunk, normalized))
        return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]

    async def _dense(self, query: str, limit: int) -> list[RetrievedChunk]:
        return await asyncio.to_thread(self._dense_sync, query, limit)

    def _dense_sync(self, query: str, limit: int) -> list[RetrievedChunk]:
        if not self._dense_model_name:
            return []
        try:
            from sentence_transformers import SentenceTransformer
        except Exception:
            return []

        try:
            if self._dense_model is None:
                self._dense_model = SentenceTransformer(self._dense_model_name)
            if self._dense_vectors is None:
                self._dense_vectors = [
                    self._dense_model.encode(chunk.content, normalize_embeddings=True).tolist()
                    for chunk in self._chunks
                ]
            q_vec = self._dense_model.encode(query, normalize_embeddings=True).tolist()
        except Exception:
            return []
        scored = []
        for chunk, vector in zip(self._chunks, self._dense_vectors):
            score = sum(a * b for a, b in zip(q_vec, vector))
            if score >= self._min_dense_score:
                scored.append(self._to_result(chunk, score))
        return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]

    def _rrf_merge(self, ranked_lists: Iterable[list[RetrievedChunk]]) -> list[RetrievedChunk]:
        scores: dict[str, float] = {}
        relevance: dict[str, list[float]] = defaultdict(list)
        docs: dict[str, RetrievedChunk] = {}
        for ranked in ranked_lists:
            seen: set[str] = set()
            for rank, item in enumerate(ranked, start=1):
                key = item.chunk_id or item.content[:100]
                if key in seen:
                    continue
                seen.add(key)
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
                relevance[key].append(float(item.score))
                docs.setdefault(key, item)
        merged = []
        for key in sorted(scores, key=lambda item: (scores[item], max(relevance[item])), reverse=True):
            item = docs[key]
            confidence = min(1.0, max(relevance[key]) + 0.02 * (len(relevance[key]) - 1))
            merged.append(RetrievedChunk(item.content, round(confidence, 4), item.source, item.title, item.chunk_id))
        return merged

    @staticmethod
    def _to_result(chunk: KnowledgeChunk, score: float) -> RetrievedChunk:
        return RetrievedChunk(
            content=chunk.content,
            score=round(float(score), 4),
            source=chunk.source,
            title=chunk.title,
            chunk_id=chunk.chunk_id,
        )
