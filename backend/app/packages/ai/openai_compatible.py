from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from ...infra.config import get_settings
from .types import LLMMessage, ModelResponse, ModelStreamEvent, ToolCall


class OpenAICompatibleProvider:
    """OpenAI-compatible Chat Completions provider.

    This is the low-level AI package. It knows provider wire format, but not
    chatbot workflows, RAG, code execution, or HTTP APIs.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        provider_name: str = "openai-compatible",
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        default_model: str = "",
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.deepseek_api_key
        self.base_url = base_url or settings.deepseek_base_url
        self.max_tokens = max_tokens or settings.model_max_tokens
        self.temperature = settings.model_temperature if temperature is None else temperature
        self.provider_name = provider_name
        self.input_cost_per_million = max(0.0, input_cost_per_million)
        self.output_cost_per_million = max(0.0, output_cost_per_million)
        self.default_model = default_model
        self.thinking_mode = settings.deepseek_thinking_mode.strip().casefold()
        self.reasoning_effort = settings.deepseek_reasoning_effort.strip().casefold()
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=max(1.0, float(settings.model_request_timeout_seconds)),
        )

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[Any],
        model: str,
        *,
        system: str | None = None,
    ) -> ModelResponse:
        if not self.api_key:
            return ModelResponse(content=self._offline_response(messages))

        payload_messages = self._messages_to_openai(messages, system=system)
        response = await self.client.chat.completions.create(
            model=model,
            messages=payload_messages,
            tools=[self._tool_to_openai(tool) for tool in tools] if tools else None,
            tool_choice="auto" if tools else None,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            **self._thinking_options(model),
        )
        choice = response.choices[0]
        message = choice.message
        tool_calls = []
        for item in message.tool_calls or []:
            raw_arguments = item.function.arguments or "{}"
            try:
                args = json.loads(raw_arguments)
            except json.JSONDecodeError:
                args = {"_raw_arguments": raw_arguments}
            tool_calls.append(ToolCall(id=item.id, name=item.function.name, input=args))
        reasoning = getattr(message, "reasoning_content", None)
        return ModelResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            metadata={
                "finish_reason": choice.finish_reason,
                "reasoning_chars": len(str(reasoning or "")),
                "reasoning_content": str(reasoning or ""),
                "usage": self._usage_payload(response.usage, payload_messages, message.content or ""),
            },
        )

    async def stream_complete(
        self,
        messages: list[LLMMessage],
        tools: list[Any],
        model: str,
        *,
        system: str | None = None,
    ):
        if not self.api_key:
            response = ModelResponse(content=self._offline_response(messages))
            yield ModelStreamEvent("content_delta", delta=str(response.content))
            yield ModelStreamEvent("response", response=response)
            return

        payload_messages = self._messages_to_openai(messages, system=system)
        stream = await self.client.chat.completions.create(
            model=model,
            messages=payload_messages,
            tools=[self._tool_to_openai(tool) for tool in tools] if tools else None,
            tool_choice="auto" if tools else None,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=True,
            **self._thinking_options(model),
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = None
        calls: dict[int, dict[str, str]] = {}
        stream_usage = None
        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                stream_usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_delta = str(reasoning)
                reasoning_parts.append(reasoning_delta)
                yield ModelStreamEvent("thinking_delta", delta=reasoning_delta)
            if delta.content:
                content_parts.append(delta.content)
                yield ModelStreamEvent("content_delta", delta=delta.content)
            for item in delta.tool_calls or []:
                index = int(item.index or 0)
                builder = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if item.id:
                    builder["id"] += item.id
                if item.function:
                    if item.function.name:
                        builder["name"] += item.function.name
                    if item.function.arguments:
                        builder["arguments"] += item.function.arguments
        tool_calls: list[ToolCall] = []
        for index in sorted(calls):
            item = calls[index]
            try:
                arguments = json.loads(item["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw_arguments": item["arguments"]}
            tool_calls.append(ToolCall(item["id"] or f"call-{index}", item["name"], arguments))
        yield ModelStreamEvent(
            "response",
            response=ModelResponse(
                content="".join(content_parts),
                tool_calls=tool_calls,
                metadata={
                    "finish_reason": finish_reason,
                    "reasoning_chars": sum(len(item) for item in reasoning_parts),
                    "reasoning_content": "".join(reasoning_parts),
                    "usage": self._usage_payload(stream_usage, payload_messages, "".join(content_parts)),
                },
            ),
        )

    @staticmethod
    def assistant_tool_content(
        content: Any,
        tool_calls: list[ToolCall],
        reasoning_content: str = "",
    ) -> dict[str, Any]:
        message = {
            "role": "assistant",
            "content": str(content) if content else None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.input, ensure_ascii=False)},
                }
                for call in tool_calls
            ],
        }
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        return message

    def _thinking_options(self, model: str) -> dict[str, Any]:
        """Return DeepSeek V4 thinking controls without affecting other providers."""
        if "api.deepseek.com" not in self.base_url.casefold() and not model.casefold().startswith("deepseek-"):
            return {}
        mode = self.thinking_mode if self.thinking_mode in {"enabled", "disabled"} else "disabled"
        options: dict[str, Any] = {"extra_body": {"thinking": {"type": mode}}}
        if mode == "enabled":
            effort = self.reasoning_effort if self.reasoning_effort in {"low", "high", "max"} else "low"
            options["reasoning_effort"] = effort
        return options

    def _usage_payload(self, usage: Any, messages: list[dict[str, Any]], output: str) -> dict[str, Any]:
        if usage is not None:
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            details = getattr(usage, "prompt_tokens_details", None)
            cache_read = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            estimated = False
        else:
            input_tokens = max(1, len(json.dumps(messages, ensure_ascii=False, default=str)) // 4)
            output_tokens = max(1, len(str(output)) // 4) if output else 0
            cache_read = 0
            estimated = True
        cost = (
            input_tokens * self.input_cost_per_million
            + output_tokens * self.output_cost_per_million
        ) / 1_000_000
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": 0,
            "cost_usd": round(cost, 10),
            "estimated": estimated,
        }

    @staticmethod
    def _messages_to_openai(messages: list[LLMMessage], *, system: str | None) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        if system:
            converted.append({"role": "system", "content": system})
        for message in messages:
            if message.role == "assistant" and isinstance(message.content, dict):
                converted.append(message.content)
            elif message.role == "tool":
                converted.append({
                    "role": "tool",
                    "tool_call_id": str(message.content.get("tool_call_id", "")),
                    "content": str(message.content.get("content", "")),
                })
            else:
                converted.append({"role": message.role, "content": str(message.content)})
        return converted

    @staticmethod
    def _tool_to_openai(tool: Any) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }

    @staticmethod
    def _offline_response(messages: list[LLMMessage]) -> str:
        tool_context = [
            str(message.content.get("content", ""))
            for message in messages
            if message.role == "tool" and isinstance(message.content, dict)
        ]
        if tool_context:
            return "LLM API key is not configured. Tool context:\n\n" + "\n\n---\n\n".join(tool_context)[:4000]
        user_messages = [message.content for message in messages if message.role == "user"]
        return "LLM API key is not configured. Last message: " + str(user_messages[-1] if user_messages else "")
