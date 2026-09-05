from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from ...infra.config import get_settings
from .anthropic_compatible import AnthropicCompatibleProvider
from .openai_compatible import OpenAICompatibleProvider


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    base_url: str
    api_key: str
    default_model: str
    auth_token: str = ""
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    capabilities: tuple[str, ...] = ("text", "streaming", "tools")
    protocol: str = "openai"

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("api_key", None)
        payload.pop("auth_token", None)
        payload["configured"] = bool(self.api_key or self.auth_token)
        return payload


class ProviderRegistry:
    """Configuration-driven registry for OpenAI-compatible model providers."""

    def __init__(self) -> None:
        settings = get_settings()
        self._specs: dict[str, ProviderSpec] = {
            "deepseek": ProviderSpec(
                id="deepseek",
                base_url=settings.deepseek_base_url,
                api_key=settings.deepseek_api_key,
                default_model=settings.deepseek_model,
                input_cost_per_million=settings.model_input_cost_per_million,
                output_cost_per_million=settings.model_output_cost_per_million,
                capabilities=("text", "streaming", "tools", "reasoning"),
            ),
            "anthropic": ProviderSpec(
                id="anthropic",
                base_url=settings.anthropic_base_url,
                api_key=settings.anthropic_api_key,
                auth_token=settings.anthropic_auth_token,
                default_model=settings.anthropic_model,
                capabilities=("text", "streaming", "tools", "reasoning"),
                protocol="anthropic",
            ),
        }
        self._load_json(settings.model_providers_json)

    def _load_json(self, raw: str) -> None:
        if not raw.strip():
            return
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(decoded, dict):
            entries = []
            for key, value in decoded.items():
                if isinstance(value, dict):
                    entries.append({"id": key, **value})
        else:
            entries = decoded
        if not isinstance(entries, list):
            return
        for item in entries:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip().casefold()
            base_url = str(item.get("base_url") or "").strip()
            model = str(item.get("default_model") or item.get("model") or "").strip()
            if not provider_id or not base_url or not model:
                continue
            capabilities = tuple(str(value) for value in (item.get("capabilities") or ["text", "streaming", "tools"]))
            self._specs[provider_id] = ProviderSpec(
                id=provider_id,
                base_url=base_url,
                api_key=str(item.get("api_key") or ""),
                auth_token=str(item.get("auth_token") or ""),
                default_model=model,
                input_cost_per_million=float(item.get("input_cost_per_million") or 0.0),
                output_cost_per_million=float(item.get("output_cost_per_million") or 0.0),
                capabilities=capabilities,
                protocol=str(item.get("protocol") or "openai").casefold(),
            )

    def spec(self, provider_id: str) -> ProviderSpec:
        normalized = provider_id.strip().casefold()
        if normalized not in self._specs:
            raise ValueError(f"Unknown model provider: {provider_id}")
        return self._specs[normalized]

    def create(self, provider_id: str):
        spec = self.spec(provider_id)
        if spec.protocol == "anthropic":
            return AnthropicCompatibleProvider(
                api_key=spec.api_key,
                auth_token=spec.auth_token,
                base_url=spec.base_url,
                default_model=spec.default_model,
                provider_name=spec.id,
                input_cost_per_million=spec.input_cost_per_million,
                output_cost_per_million=spec.output_cost_per_million,
            )
        return OpenAICompatibleProvider(
            api_key=spec.api_key,
            base_url=spec.base_url,
            provider_name=spec.id,
            input_cost_per_million=spec.input_cost_per_million,
            output_cost_per_million=spec.output_cost_per_million,
            default_model=spec.default_model,
        )

    def list_public(self) -> list[dict[str, Any]]:
        return [self._specs[key].public_dict() for key in sorted(self._specs)]
