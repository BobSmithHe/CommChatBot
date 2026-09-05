from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=4)
def get_embedding_model(model_name: str, device: str):
    """Share heavyweight embedding models across RAG and memory indexes."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, device=device)
