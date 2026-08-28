from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..ai import ModelProvider, OpenAICompatibleProvider
from ...core.hooks import hook_runner
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
        prompt: str,
        history: list[AgentMessage] | None = None,
        resume_state: dict | None = None,
    ) -> AsyncIterator[AgentEvent]:
        resumed = bool(resume_state)
        if resume_state:
            raw_messages = resume_state.get("messages") or []
            self.messages = [AgentMessage.from_dict(item) for item in raw_messages if isinstance(item, dict)]
            start_turn = max(0, int(resume_state.get("next_turn") or 0))
        else:
            self.messages = list(history or [])
            user_message = AgentMessage.user(prompt)
            self.messages.append(user_message)
            start_turn = 0
            self._save_checkpoint(start_turn)
            yield AgentEvent("message_end", {"message": user_message})

        try:
            for result in await hook_runner.run(
                "SessionStart",
                {"task_id": self.task_id, "reason": "resume" if resumed else "startup"},
                self.workspace_dir,
            ):
                yield AgentEvent("hook", result)

            executor = ToolExecutor(self.tools)
            tools_by_name = {tool.name: tool for tool in self.tools}
            for turn_index in range(start_turn, self.max_turns):
                runtime_task_manager.ensure_not_cancelled(self.task_id)
                self._save_checkpoint(turn_index)
                yield AgentEvent("turn_start", {"index": turn_index, "resumed": resumed})
                response = None
                message_started = False
                stream_method = getattr(self.provider, "stream_complete", None)
                if callable(stream_method):
                    async for stream_event in stream_method(
                        convert_to_llm(self.messages), self.tools, self.model, system=self.system_prompt
                    ):
                        runtime_task_manager.ensure_not_cancelled(self.task_id)
                        if stream_event.type == "content_delta" and stream_event.delta:
                            if not message_started:
                                yield AgentEvent("message_start", {"message": None})
                                message_started = True
                            yield AgentEvent("message_update", {"delta": stream_event.delta, "message": None})
                        elif stream_event.type == "response":
                            response = stream_event.response
                else:
                    response = await self.provider.complete(
                        convert_to_llm(self.messages), self.tools, self.model, system=self.system_prompt
                    )
                if response is None:
                    raise RuntimeError("Model provider did not return a final response")

                if not response.tool_calls:
                    assistant = AgentMessage.assistant(response.content)
                    self.messages.append(assistant)
                    self._save_checkpoint(turn_index + 1)
                    if not message_started:
                        yield AgentEvent("message_start", {"message": assistant})
                    if response.content and not message_started:
                        yield AgentEvent("message_update", {"delta": str(response.content), "message": assistant})
                    yield AgentEvent("message_end", {"message": assistant})
                    yield AgentEvent("turn_end", {"index": turn_index, "tool_calls": 0})
                    async for event in self._stop_events("completed"):
                        yield event
                    return

                assistant = AgentMessage.assistant(
                    OpenAICompatibleProvider.assistant_tool_content(response.content, response.tool_calls)
                )
                self.messages.append(assistant)
                yield AgentEvent("message_end", {"message": assistant})

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

                executed = await self._execute_prepared(prepared, executor)
                for _call, tool, result in executed:
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
                    yield AgentEvent("tool_end", {"result": result})
                    yield AgentEvent("message_end", {"message": result_message})
                self._save_checkpoint(turn_index + 1)
                yield AgentEvent("turn_end", {"index": turn_index, "tool_calls": len(response.tool_calls)})

            yield AgentEvent("error", {"error": f"AgentRuntime exceeded max_turns={self.max_turns}"})
            async for event in self._stop_events("max_turns"):
                yield event
        except TaskCancelled:
            async for event in self._stop_events("cancelled"):
                yield event
            raise
        except asyncio.CancelledError:
            await hook_runner.run("Stop", {"task_id": self.task_id, "reason": "interrupted"}, self.workspace_dir)
            raise
        except Exception as exc:
            async for event in self._stop_events("error", error=f"{type(exc).__name__}: {exc}"):
                yield event
            raise

    def _save_checkpoint(self, next_turn: int) -> None:
        if not self.task_id:
            return
        runtime_task_manager.save_checkpoint(
            self.task_id,
            {
                "version": 1,
                "next_turn": next_turn,
                "messages": [message.to_dict() for message in self.messages],
            },
        )

    async def _execute_prepared(self, prepared: list[tuple], executor: ToolExecutor) -> list[tuple]:
        executed: list[tuple] = []
        index = 0
        while index < len(prepared):
            call, tool, immediate = prepared[index]
            if immediate is not None:
                executed.append((call, tool, immediate))
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
                executed.extend((item[0], item[1], result) for item, result in zip(batch, results))
                continue
            executed.append((call, tool, await executor.execute(call)))
            index += 1
        return executed

    async def _stop_events(self, reason: str, error: str | None = None) -> AsyncIterator[AgentEvent]:
        payload = {"task_id": self.task_id, "reason": reason}
        if error:
            payload["error"] = error
        for result in await hook_runner.run("Stop", payload, self.workspace_dir):
            yield AgentEvent("hook", result)
