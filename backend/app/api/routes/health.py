from __future__ import annotations

from fastapi import APIRouter

from ...infra.config import get_settings
from ...infra.integrations import get_external_integrations
from ...services import get_extension_registry, get_provider_registry
from ...products.profiles import CHAT_PROFILE, CODING_PROFILE, apply_profile_overrides

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


@router.get("/api/extensions")
def extension_catalog() -> dict:
    settings = get_settings()
    chat_profile = apply_profile_overrides(CHAT_PROFILE, settings.chat_extensions)
    coding_profile = apply_profile_overrides(CODING_PROFILE, settings.coding_extensions)
    manifests = get_extension_registry().manifests()
    return {
        "profiles": {
            chat_profile.id: list(chat_profile.extensions),
            coding_profile.id: list(coding_profile.extensions),
        },
        "extensions": [{
            "id": manifest.id,
            "version": manifest.version,
            "modes": sorted(manifest.modes),
            "requires": list(manifest.requires),
            "description": manifest.description,
            "schema_version": manifest.schema_version,
            "runtime_api": manifest.runtime_api,
            "permissions": sorted(manifest.permissions),
            "config_schema": dict(manifest.config_schema),
        } for manifest in manifests],
    }
