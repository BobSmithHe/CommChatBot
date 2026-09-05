from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..ai import ModelProvider, ModelResponse, OpenAICompatibleProvider, ToolCall
from ...core.hooks import hook_runner
from ...core.context import compact_llm_messages
from ...infra.config import get_settings
from ...core.runtime_state import TaskCancelled, runtime_task_manager
from .events import AgentEvent
from .messages import AgentMessage, convert_to_llm
from .tools import Tool, ToolExecutor, ToolResult


class AgentRuntime:
    """Reusable MiniCode-style runtime.

    This layer only implements the agent loop. Product modes decide prompts,
    tools, persistence, and API event mapping.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        model: str,
        tools: list[Tool],
        system_prompt: str,
        max_turns: int = 8,
        task_id: str | None = None,
        permission_mode: str = "workspace-write",
        workspace_dir: str | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.task_id = task_id
        self.permission_mode = permission_mode
        self.workspace_dir = workspace_dir
        self.messages: list[AgentMessage] = []

    async def run(
        self,
        prompt: str | None = None,
        history: list[AgentMessage] | None = None,
        resume_state: dict | None = None,
    ) -> AsyncIterator[AgentEvent]:
        resumed = resume_state is not None
        recovery_results: list[tuple[ToolCall, ToolResult, AgentMessage]] = []
        resume_phase = "before_llm"
        if resumed:
            resume_state = resume_state or {}
            raw_messages = resume_state.get("messages") or []
            self.messages = [AgentMessage.from_dict(item) for item in raw_messages if isinstance(item, dict)]
            start_turn = max(0, int(resume_state.get("next_turn") or 0))
            resume_phase = str(resume_state.get("phase") or "before_llm")
            if resume_phase == "tools_pending":
                recovery_results = self._restore_pending_tool_results(
                    resume_state.get("pending_tool_calls") or []
                )
                start_turn += 1
                self._save_checkpoint(start_turn, phase="after_tools")
        else:
            if prompt is None:
                raise ValueError("prompt is required when resume_state is not provided")
            self.messages = list(history or [])
            user_message = AgentMessage.user(prompt)
            self.messages.append(user_message)
            start_turn = 0
            self._save_checkpoint(start_turn, phase="before_llm")
            yield AgentEvent("message_end", {"message": user_message, "message_id": user_message.id})

        if resumed and prompt is not None:
            user_message = AgentMessage.user(prompt)
            self.messages.append(user_message)
            resume_phase = "before_llm"
            self._save_checkpoint(start_turn, phase="before_llm")
            yield AgentEvent("message_end", {"message": user_message, "message_id": user_message.id})

        try:
            for result in await hook_runner.run(
                "SessionStart",
                {"task_id": self.task_id, "reason": "resume" if resumed else "startup"},
                self.workspace_dir,
            ):
                yield AgentEvent("hook", result)

            for call, result, result_message in recovery_results:
                yield AgentEvent("tool_start", {
                    "id": call.id,
                    "name": call.name,
                    "input": call.input,
                    "recovered": True,
                })
                yield AgentEvent("tool_end", {"result": result, "recovered": True})
                yield AgentEvent("message_end", {
                    "message": result_message,
                    "message_id": result_message.id,
                })

            if resumed and resume_phase == "completed" and prompt is None:
                assistant = next(
                    (message for message in reversed(self.messages) if message.type == "assistant"),
                    None,
                )
                if assistant is None or not str(assistant.content or "").strip():
                    raise RuntimeError("Completed checkpoint has no assistant response")
                yield AgentEvent("message_start", {"message": assistant, "message_id": assistant.id})
                yield AgentEvent("message_update", {
                    "delta": str(assistant.content),
                    "message": assistant,
                    "message_id": assistant.id,
                    "final": True,
                })
                yield AgentEvent("message_end", {"message": assistant, "message_id": assistant.id})
                yield AgentEvent("final_answer", {"content": str(assistant.content), "message_id": assistant.id})
                async for event in self._stop_events("completed"):
                    yield event
                return

            executor = ToolExecutor(self.tools)
            tools_by_name = {tool.name: tool for tool in self.tools}
            turn_index = start_turn
            turn_limit = self.max_turns
            while turn_index < turn_limit:
                runtime_task_manager.ensure_not_cancelled(self.task_id)
                queued_steering = self._consume_mailbox(("steering",))
                if queued_steering:
                    turn_limit += 1
                    for item, queued_message in queued_steering:
                        self.messages.append(queued_message)
                        yield AgentEvent("queued_message", {
                            "id": item["id"],
                            "kind": item["kind"],
                            "content": item["content"],
                            "message_id": queued_message.id,
                        })
                        yield AgentEvent("message_end", {"message": queued_message, "message_id": queued_message.id})
                self._save_checkpoint(turn_index, phase="before_llm")
                yield AgentEvent("turn_start", {"index": turn_index, "resumed": resumed})
                response = None
                assistant = AgentMessage.assistant("")
                streamed_content = False
                streamed_thinking = False
                yield AgentEvent("message_start", {"message": assistant, "message_id": assistant.id})
                stream_method = getattr(self.provider, "stream_complete", None)
                settings = get_settings()
                compaction = await compact_llm_messages(
                    convert_to_llm(self.messages),
                    provider=self.provider,
                    model=self.model,
                    max_tokens=settings.model_context_tokens - settings.model_max_tokens - 1024,
                )
                llm_messages = compaction.messages
                if compaction.compacted:
                    yield AgentEvent("context_compacted", {
                        "tokens_before": compaction.tokens_before,
                        "tokens_after": compaction.tokens_after,
                        "summary": compaction.summary,
                    })
                    runtime_task_manager.record_usage(
                        self.task_id,
                        str(getattr(self.provider, "provider_name", type(self.provider).__name__)),
                        self.model,
                        compaction.usage,
                    )
                if callable(stream_method):
                    async for stream_event in stream_method(
                        llm_messages, self.tools, self.model, system=self.system_prompt
                    ):
                        runtime_task_manager.ensure_not_cancelled(self.task_id)
                        if stream_event.type == "content_delta" and stream_event.delta:
                            streamed_content = True
                            assistant.content = str(assistant.content or "") + stream_event.delta
                            yield AgentEvent("message_update", {
                                "delta": stream_event.delta,
                                "message": assistant,
                                "message_id": assistant.id,
                                "final": not bool(self.tools),
                            })
                        elif stream_event.type == "thinking_delta" and stream_event.delta:
                            streamed_thinking = True
                            yield AgentEvent("thinking", {
                                "delta": stream_event.delta,
                                "message_id": assistant.id,
                            })
                        elif stream_event.type == "response":
                            response = stream_event.response
                else:
                    response = await self.provider.complete(
                        llm_messages, self.tools, self.model, system=self.system_prompt
                    )
                if response is None:
                    raise RuntimeError("Model provider did not return a final response")

                if not response.tool_calls and not str(response.content or "").strip():
                    yield AgentEvent("status", {
                        "message": "Model returned an empty response; retrying this turn",
                        "finish_reason": response.metadata.get("finish_reason"),
                    })
                    recovery = await self.provider.complete(
                        llm_messages,
                        self.tools,
                        self.model,
                        system=(
                            self.system_prompt
                            + "\n\nThe previous attempt ended without visible text or tool calls. "
                            "Continue the task now. If work remains, use the available tools. "
                            "Otherwise return a concise final Markdown response. Never return an empty response."
                        ),
                    )
                    recovery.metadata = {**response.metadata, **recovery.metadata, "empty_response_retry": True}
                    response = recovery

                if not response.tool_calls and not str(response.content or "").strip():
                    finish_reason = response.metadata.get("finish_reason") or "unknown"
                    raise RuntimeError(
                        f"Model returned an empty response after retry (finish_reason={finish_reason})"
                    )

                reasoning_content = str(response.metadata.get("reasoning_content") or "")
                assistant.metadata["reasoning_content"] = reasoning_content
                if reasoning_content and not streamed_thinking:
                    yield AgentEvent("thinking", {
                        "delta": reasoning_content,
                        "message_id": assistant.id,
                    })

                if response.content and not streamed_content:
                    assistant.content = response.content
                    streamed_content = True
                    yield AgentEvent("message_update", {
                        "delta": str(response.content),
                        "message": assistant,
                        "message_id": assistant.id,
                        "final": not bool(self.tools),
                    })

                yield AgentEvent(
                    "llm_response",
                    self._llm_response_payload(response, turn_index, assistant.id),
                )
                runtime_task_manager.record_usage(
                    self.task_id,
                    str(getattr(self.provider, "provider_name", type(self.provider).__name__)),
                    self.model,
                    dict(response.metadata.get("usage") or {}),
                )

                if not response.tool_calls:
                    assistant.content = response.content
                    self.messages.append(assistant)
                    queued = self._consume_mailbox(("steering", "follow_up"))
                    if queued:
                        if str(response.content or "").strip():
                            yield AgentEvent("intermediate_answer", {
                                "content": str(response.content),
                                "message_id": assistant.id,
                            })
                        yield AgentEvent("message_end", {"message": assistant, "message_id": assistant.id})
                        yield AgentEvent("turn_end", {
                            "index": turn_index,
                            "tool_calls": 0,
                            "continued_by": [item["kind"] for item, _ in queued],
                        })
                        for item, queued_message in queued:
                            self.messages.append(queued_message)
                            yield AgentEvent("queued_message", {
                                "id": item["id"],
                                "kind": item["kind"],
                                "content": item["content"],
                                "message_id": queued_message.id,
                            })
                            yield AgentEvent("message_end", {
                                "message": queued_message,
                                "message_id": queued_message.id,
                            })
                        turn_index += 1
                        turn_limit += 1
                        self._save_checkpoint(turn_index, phase="before_llm")
                        continue
                    self._save_checkpoint(turn_index + 1, phase="completed")
                    yield AgentEvent("message_end", {"message": assistant, "message_id": assistant.id})
                    if self.tools and response.content:
                        yield AgentEvent("final_answer", {
                            "content": str(response.content),
                            "message_id": assistant.id,
                        })
                    yield AgentEvent("turn_end", {"index": turn_index, "tool_calls": 0})
                    async for event in self._stop_events("completed"):
                        yield event
                    return

                assistant.content = OpenAICompatibleProvider.assistant_tool_content(
                    response.content,
                    response.tool_calls,
                    str(response.metadata.get("reasoning_content") or ""),
                )
                self.messages.append(assistant)
                pending_calls = list(response.tool_calls)
                self._save_checkpoint(
                    turn_index,
                    phase="tools_pending",
                    pending_tool_calls=pending_calls,
                )
                yield AgentEvent("message_end", {"message": assistant, "message_id": assistant.id})

                prepared: list[tuple] = []
                for call in response.tool_calls:
                    runtime_task_manager.ensure_not_cancelled(self.task_id)
                    yield AgentEvent("tool_start", {"id": call.id, "name": call.name, "input": call.input})
                    tool = tools_by_name.get(call.name)
                    if tool is None:
                        prepared.append((call, None, await executor.execute(call)))
                        continue

                    hook_results = await hook_runner.run(
                        "PreToolUse",
                        {"task_id": self.task_id, "tool_name": tool.name, "input": call.input},
                        self.workspace_dir,
                    )
                    for hook_result in hook_results:
                        yield AgentEvent("hook", hook_result)
                    blocked = next((item for item in hook_results if item.get("allow") is False), None)
                    if blocked:
                        prepared.append((call, tool, ToolResult(
                            call.id, call.name, str(blocked.get("reason") or "Blocked by hook"), is_error=True
                        )))
                        continue

                    action = runtime_task_manager.permission_action(self.permission_mode, tool.capability)
                    if action == "deny":
                        prepared.append((call, tool, ToolResult(
                            call.id, call.name,
                            f"Permission mode {self.permission_mode} denies {tool.capability}",
                            is_error=True,
                        )))
                        continue
                    if action == "approve" and self.task_id:
                        approval_id = runtime_task_manager.create_approval(
                            self.task_id, tool.name, call.input, tool.capability
                        )
                        approval_payload = {
                            "approval_id": approval_id,
                            "task_id": self.task_id,
                            "tool_name": tool.name,
                            "input": call.input,
                            "capability": tool.capability,
                        }
                        yield AgentEvent("approval_required", approval_payload)
                        yield AgentEvent("tool_update", {
                            "id": call.id,
                            "name": call.name,
                            "stage": "waiting_approval",
                        })
                        permission_hooks = await hook_runner.run(
                            "PermissionRequest", approval_payload, self.workspace_dir
                        )
                        for hook_result in permission_hooks:
                            yield AgentEvent("hook", hook_result)
                        hook_rejection = next(
                            (item for item in permission_hooks if item.get("allow") is False), None
                        )
                        if hook_rejection:
                            runtime_task_manager.resolve_approval(approval_id, False)
                            approved = False
                        else:
                            approved = await runtime_task_manager.wait_for_approval(approval_id)
                        runtime_task_manager.ensure_not_cancelled(self.task_id)
                        if not approved:
                            prepared.append((call, tool, ToolResult(
                                call.id, call.name, "User or hook rejected the request, or approval expired", is_error=True
                            )))
                            continue
                    prepared.append((call, tool, None))

                for call, tool, immediate in prepared:
                    if tool is not None and immediate is None:
                        yield AgentEvent("tool_update", {
                            "id": call.id,
                            "name": call.name,
                            "stage": "running",
                        })
                async for call, tool, result in self._execute_prepared(prepared, executor):
                    if tool is not None:
                        for hook_result in await hook_runner.run(
                            "PostToolUse",
                            {
                                "task_id": self.task_id,
                                "tool_name": tool.name,
                                "result": result.content,
                                "is_error": result.is_error,
                            },
                            self.workspace_dir,
                        ):
                            yield AgentEvent("hook", hook_result)
                    result_message = AgentMessage(
                        "tool_result",
                        result.content,
                        tool_call_id=result.tool_call_id,
                        tool_name=result.tool_name,
                        is_error=result.is_error,
                        metadata=result.metadata,
                    )
                    self.messages.append(result_message)
                    yield AgentEvent("tool_update", {
                        "id": call.id,
                        "name": call.name,
                        "stage": "failed" if result.is_error else "completed",
                        "is_error": result.is_error,
                        "metadata": result.metadata,
                    })
                    yield AgentEvent("tool_end", {"result": result})
                    yield AgentEvent("message_end", {
                        "message": result_message,
                        "message_id": result_message.id,
                    })
                    pending_calls = [item for item in pending_calls if item.id != call.id]
                    self._save_checkpoint(
                        turn_index,
                        phase="tools_pending",
                        pending_tool_calls=pending_calls,
                    )
                queued = self._consume_mailbox(("steering",))
                if queued:
                    turn_limit += 1
                    for item, queued_message in queued:
                        self.messages.append(queued_message)
                        yield AgentEvent("queued_message", {
                            "id": item["id"],
                            "kind": item["kind"],
                            "content": item["content"],
                            "message_id": queued_message.id,
                        })
                        yield AgentEvent("message_end", {
                            "message": queued_message,
                            "message_id": queued_message.id,
                        })
                self._save_checkpoint(turn_index + 1, phase="after_tools")
                yield AgentEvent("turn_end", {"index": turn_index, "tool_calls": len(response.tool_calls)})
                turn_index += 1

            yield AgentEvent("error", {"error": f"AgentRuntime exceeded max_turns={self.max_turns}"})
            async for event in self._stop_events("max_turns"):
                yield event
        except TaskCancelled:
            async for event in self._stop_events("cancelled"):
                yield event
            # The durable task executor relies on this sentinel to perform the
            # sole running -> cancelled state transition. It is never reported
            # as an error event or HTTP 500 by the queue/SSE layer.
            raise
        except asyncio.CancelledError:
            await hook_runner.run("Stop", {"task_id": self.task_id, "reason": "interrupted"}, self.workspace_dir)
            raise
        except Exception as exc:
            async for event in self._stop_events("error", error=f"{type(exc).__name__}: {exc}"):
                yield event
            raise

    def _save_checkpoint(
        self,
        next_turn: int,
        *,
        phase: str = "before_llm",
        pending_tool_calls: list[ToolCall] | None = None,
    ) -> None:
        if not self.task_id:
            return
        runtime_task_manager.save_checkpoint(
            self.task_id,
            {
                "version": 2,
                "next_turn": next_turn,
                "phase": phase,
                "messages": [message.to_dict() for message in self.messages],
                "pending_tool_calls": [
                    {"id": call.id, "name": call.name, "input": call.input}
                    for call in (pending_tool_calls or [])
                ],
            },
        )

    async def _execute_prepared(
        self,
        prepared: list[tuple],
        executor: ToolExecutor,
    ) -> AsyncIterator[tuple]:
        index = 0
        while index < len(prepared):
            call, tool, immediate = prepared[index]
            if immediate is not None:
                yield call, tool, immediate
                index += 1
                continue
            if tool is not None and tool.execution_mode == "parallel":
                batch = []
                while index < len(prepared):
                    batch_call, batch_tool, batch_immediate = prepared[index]
                    if batch_immediate is not None or batch_tool is None or batch_tool.execution_mode != "parallel":
                        break
                    batch.append((batch_call, batch_tool))
                    index += 1
                results = await asyncio.gather(*(executor.execute(item[0]) for item in batch))
                for item, result in zip(batch, results):
                    yield item[0], item[1], result
                continue
            yield call, tool, await executor.execute(call)
            index += 1

    async def _stop_events(self, reason: str, error: str | None = None) -> AsyncIterator[AgentEvent]:
        payload = {"task_id": self.task_id, "reason": reason}
        if error:
            payload["error"] = error
        for result in await hook_runner.run("Stop", payload, self.workspace_dir):
            yield AgentEvent("hook", result)

    @staticmethod
    def _llm_response_payload(response: ModelResponse, turn_index: int, message_id: str) -> dict:
        return {
            "turn": turn_index,
            "message_id": message_id,
            "text": str(response.content or ""),
            "tool_calls": [
                {"name": call.name, "input": call.input}
                for call in response.tool_calls
            ],
            "finish_reason": response.metadata.get("finish_reason"),
            "recovered": bool(response.metadata.get("empty_response_retry")),
        }

    def _restore_pending_tool_results(
        self,
        pending: list[dict],
    ) -> list[tuple[ToolCall, ToolResult, AgentMessage]]:
        existing = {
            message.tool_call_id
            for message in self.messages
            if message.type == "tool_result" and message.tool_call_id
        }
        restored: list[tuple[ToolCall, ToolResult, AgentMessage]] = []
        for item in pending:
            if not isinstance(item, dict):
                continue
            call = ToolCall(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or "unknown"),
                input=dict(item.get("input") or {}),
            )
            if not call.id or call.id in existing:
                continue
            content = (
                "Tool execution was interrupted by a runtime restart. Its outcome is uncertain, "
                "so the operation was not automatically repeated. Inspect workspace state and "
                "retry only if needed. Ephemeral terminal sessions must be recreated."
            )
            result = ToolResult(
                call.id,
                call.name,
                content,
                is_error=True,
                metadata={"recovery_uncertain": True, "not_replayed": True},
            )
            result_message = AgentMessage(
                "tool_result",
                result.content,
                tool_call_id=result.tool_call_id,
                tool_name=result.tool_name,
                is_error=True,
                metadata=result.metadata,
            )
            self.messages.append(result_message)
            restored.append((call, result, result_message))
        return restored

    def _consume_mailbox(self, kinds: tuple[str, ...]) -> list[tuple[dict, AgentMessage]]:
        if not self.task_id:
            return []
        return [
            (item, AgentMessage.user(str(item["content"])))
            for item in runtime_task_manager.consume_messages(self.task_id, kinds)
        ]
