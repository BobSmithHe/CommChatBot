from __future__ import annotations

from fastapi import APIRouter

from ...infra.config import get_settings
from ...infra.integrations import get_external_integrations
from ...services import get_provider_registry

router = APIRouter()


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@router.get("/health/integrations")
def integration_health() -> dict:
    return {"status": "ok", "integrations": get_external_integrations().health()}


@router.get("/api/models")
def model_catalog() -> dict:
    settings = get_settings()
    return {
        "chat_provider": settings.chat_provider_id,
        "coding_provider": settings.coding_provider_id,
        "providers": get_provider_registry().list_public(),
    }
