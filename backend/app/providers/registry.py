from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from collections.abc import Iterable
from typing import Any

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
    max_tokens: int = 4096
    temperature: float = 0.3
    request_timeout_seconds: float = 90.0
    thinking_mode: str = "enabled"
    reasoning_effort: str = "low"

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("api_key", None)
        payload.pop("auth_token", None)
        payload["configured"] = bool(self.api_key or self.auth_token)
        return payload


class ProviderRegistry:
    """Provider catalog with no dependency on application configuration.

    The application composition root translates environment settings into
    ``ProviderSpec`` instances.  Keeping that translation outside this module
    makes the provider package usable by the standalone runtime SDK.
    """

    def __init__(
        self,
        specs: Iterable[ProviderSpec] | None = None,
        *,
        providers_json: str = "",
    ) -> None:
        defaults = specs if specs is not None else (
            ProviderSpec(
                id="deepseek", base_url="https://api.deepseek.com/v1",
                api_key="", default_model="deepseek-chat",
                capabilities=("text", "streaming", "tools", "reasoning"),
            ),
            ProviderSpec(
                id="anthropic", base_url="https://api.anthropic.com",
                api_key="", default_model="claude-sonnet-4-6",
                capabilities=("text", "streaming", "tools", "reasoning"),
                protocol="anthropic",
            ),
        )
        self._specs = {spec.id.strip().casefold(): spec for spec in defaults}
        self._load_json(providers_json)

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
                max_tokens=max(1, int(item.get("max_tokens") or 4096)),
                temperature=float(item.get("temperature") if item.get("temperature") is not None else 0.3),
                request_timeout_seconds=max(1.0, float(item.get("request_timeout_seconds") or 90.0)),
                thinking_mode=str(item.get("thinking_mode") or "enabled"),
                reasoning_effort=str(item.get("reasoning_effort") or "low"),
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
                max_tokens=spec.max_tokens,
                temperature=spec.temperature,
            )
        return OpenAICompatibleProvider(
            api_key=spec.api_key,
            base_url=spec.base_url,
            provider_name=spec.id,
            input_cost_per_million=spec.input_cost_per_million,
            output_cost_per_million=spec.output_cost_per_million,
            default_model=spec.default_model,
            max_tokens=spec.max_tokens,
            temperature=spec.temperature,
            request_timeout_seconds=spec.request_timeout_seconds,
            thinking_mode=spec.thinking_mode,
            reasoning_effort=spec.reasoning_effort,
        )

    def list_public(self) -> list[dict[str, Any]]:
        return [self._specs[key].public_dict() for key in sorted(self._specs)]
