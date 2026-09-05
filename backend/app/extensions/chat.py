"""Backward-compatible imports for the split built-in chat extensions."""

from .builtin.memory import ChatMemoryExtension
from .builtin.rag import RagExtension as ChatRetrievalExtension
from .builtin.web_search import WebSearchExtension as ChatWebSearchExtension

__all__ = ["ChatMemoryExtension", "ChatRetrievalExtension", "ChatWebSearchExtension"]
