from __future__ import annotations

import json
from typing import Any

from anthropic import AsyncAnthropic

from ...infra.config import get_settings
from .types import LLMMessage, ModelResponse, ModelStreamEvent, ToolCall


class AnthropicCompatibleProvider:
    """Anthropic Messages provider, including compatible custom base URLs."""

    def __init__(
        self,
        *,
        api_key: str,
        auth_token: str = "",
        base_url: str,
        default_model: str,
        provider_name: str = "anthropic",
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key
        self.auth_token = auth_token
        self.configured = bool(api_key or auth_token)
        self.base_url = base_url
        self.default_model = default_model
        self.provider_name = provider_name
        self.max_tokens = settings.model_max_tokens
        self.temperature = settings.model_temperature
        self.input_cost_per_million = max(0.0, input_cost_per_million)
        self.output_cost_per_million = max(0.0, output_cost_per_million)
        client_auth: dict[str, str] = {}
        if auth_token:
            client_auth["auth_token"] = auth_token
        else:
            client_auth["api_key"] = api_key or "not-configured"
        self.client = AsyncAnthropic(base_url=base_url, **client_auth)

    async def complete(self, messages, tools, model, *, system=None) -> ModelResponse:
        if not self.configured:
            return ModelResponse(content="LLM API key is not configured.")
        response = await self.client.messages.create(
            **self._request(messages, tools, model, system)
        )
        return self._response(response)

    async def stream_complete(self, messages, tools, model, *, system=None):
        if not self.configured:
            yield ModelStreamEvent(
                "response", response=ModelResponse(content="LLM API key is not configured.")
            )
            return
        emitted = False
        try:
            async with self.client.messages.stream(
                **self._request(messages, tools, model, system)
            ) as stream:
                async for event in stream:
                    if getattr(event, "type", "") != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", "")
                    if delta_type == "text_delta" and getattr(delta, "text", ""):
                        emitted = True
                        yield ModelStreamEvent("content_delta", delta=str(delta.text))
                    elif delta_type == "thinking_delta" and getattr(delta, "thinking", ""):
                        emitted = True
                        yield ModelStreamEvent("thinking_delta", delta=str(delta.thinking))
                final = await stream.get_final_message()
            yield ModelStreamEvent("response", response=self._response(final))
        except Exception:
            if emitted:
                raise
            # Some Anthropic-compatible gateways implement Messages but not
            # streaming. A pre-delta failure is safe to retry as one request.
            response = await self.complete(messages, tools, model, system=system)
            reasoning = str(response.metadata.get("reasoning_content") or "")
            if reasoning:
                yield ModelStreamEvent("thinking_delta", delta=reasoning)
            if response.content:
                yield ModelStreamEvent("content_delta", delta=str(response.content))
            yield ModelStreamEvent("response", response=response)

    def _request(self, messages, tools, model, system) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system or "",
            "messages": self._messages(messages),
        }
        if tools:
            request["tools"] = [self._tool(tool) for tool in tools]
        return request

    def _response(self, response: Any) -> ModelResponse:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(str(getattr(block, "text", "")))
            elif block_type == "thinking":
                thinking_parts.append(str(getattr(block, "thinking", "")))
            elif block_type == "tool_use":
                calls.append(ToolCall(str(block.id), str(block.name), dict(block.input or {})))
        usage = response.usage
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cost = (
            input_tokens * self.input_cost_per_million
            + output_tokens * self.output_cost_per_million
        ) / 1_000_000
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=calls,
            metadata={
                "finish_reason": getattr(response, "stop_reason", None),
                "reasoning_content": "".join(thinking_parts),
                "reasoning_chars": sum(len(item) for item in thinking_parts),
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                    "cost_usd": round(cost, 10),
                    "estimated": False,
                },
            },
        )

    @staticmethod
    def _tool(tool: Any) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []

        def append(role: str, blocks: list[dict[str, Any]]) -> None:
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})

        for message in messages:
            if message.role == "system":
                append("user", [{"type": "text", "text": "Context: " + str(message.content)}])
            elif message.role == "tool" and isinstance(message.content, dict):
                append("user", [{
                    "type": "tool_result",
                    "tool_use_id": str(message.content.get("tool_call_id") or ""),
                    "content": str(message.content.get("content") or ""),
                    "is_error": bool(message.content.get("is_error")),
                }])
            elif message.role == "assistant" and isinstance(message.content, dict):
                blocks = []
                if message.content.get("content"):
                    blocks.append({"type": "text", "text": str(message.content["content"])})
                for call in message.content.get("tool_calls") or []:
                    function = call.get("function") or {}
                    raw = function.get("arguments") or "{}"
                    try:
                        arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                    except (TypeError, json.JSONDecodeError):
                        arguments = {"_raw_arguments": str(raw)}
                    blocks.append({
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": arguments,
                    })
                append("assistant", blocks or [{"type": "text", "text": ""}])
            else:
                append("assistant" if message.role == "assistant" else "user", [{
                    "type": "text",
                    "text": str(message.content),
                }])
        return converted
