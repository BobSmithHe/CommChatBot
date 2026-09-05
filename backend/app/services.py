"""Backward-compatible service accessors.

All object construction lives in :mod:`app.bootstrap.container`. Existing
routes may keep importing these functions without creating a second graph.
"""

from .bootstrap import get_container
from .products import ProductGateway


def get_rag_store():
    return get_container().rag_store


def get_code_executor():
    return get_container().code_executor


def get_chat_attachment_store():
    return get_container().attachment_store


def get_workspace_editor():
    return get_container().workspace_editor


def get_conversation_workspace_manager():
    return get_container().workspace_manager


def get_model_provider():
    return get_container().coding_provider


def get_provider_registry():
    return get_container().provider_registry


def get_chat_model_provider():
    return get_container().chat_provider


def get_coding_model_provider():
    return get_container().coding_provider


def get_product_tool_registry():
    return get_container().tool_catalog


def get_extension_registry():
    return get_container().extension_registry


def get_agent_runtime_host():
    return get_container().runtime_host


def get_extension_services() -> dict:
    return get_container().extension_services


def get_chatbot_mode():
    return get_container().chatbot


def get_coding_agent_mode():
    return get_container().coding_agent


def get_product_gateway() -> ProductGateway:
    return get_container().product_gateway


def get_chat_orchestrator() -> ProductGateway:
    return get_container().product_gateway
