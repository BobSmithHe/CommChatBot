from __future__ import annotations

from functools import lru_cache

from ..agent_runtime import ExtensionRegistry
from ..extensions.builtin import (
    ChatMemoryExtension,
    DiagnosticsExtension,
    GitExtension,
    GitHubExtension,
    MCPExtension,
    PlanExtension,
    ProjectContextExtension,
    ProjectMemoryExtension,
    RagExtension,
    RemoteExecutionExtension,
    SubagentExtension,
    TerminalExtension,
    WebSearchExtension,
    WorkspaceExtension,
)
from ..extensions.builtin.github import GitHubIntegration
from ..extensions.builtin.mcp import MCPClientRuntime
from ..extensions.builtin.memory import DurableMemoryJobQueue, MemoryService, execute_memory_job
from ..extensions.builtin.project_context import ProjectContextLoader, ProjectSkillRuntime
from ..extensions.builtin.rag import LocalRagStore, route_rag_query
from ..extensions.builtin.remote_execution import RemoteExecutionService
from ..extensions.builtin.terminal import WorkspaceTerminalManager
from ..extensions.builtin.workspace import ConversationWorkspaceManager, WorkspaceEditor
from ..extensions.builtin.workspace.lsp import WorkspaceLspManager
from ..extensions.support import ChatAttachmentStore
from ..extensions.support.execution import CodeExecutor
from ..extensions.tool_catalog import ProductToolRegistry
from ..infra.config import Settings, get_settings
from ..platform import PlatformAgentRuntimeHost
from ..platform.services.auth import AuthRateLimiter
from ..platform.services.context import ConversationContextManager
from ..platform.services.conversations import ConversationService
from ..platform.services.event_bus import RedisRuntimeEventBus, RuntimeEventBatchWriter
from ..platform.services.hooks import HookRunner
from ..platform.services.notifications import NotificationDispatcher
from ..platform.services.scheduler import AutomationScheduler
from ..platform.services.task_executor import execute_queued_task
from ..platform.services.task_queue import DurableTaskQueue
from ..platform.services.task_runtime import RuntimeTaskManager
from ..products import ChatbotMode, CodingAgentMode, ProductGateway
from ..products.profiles import ProductRuntimeConfig
from ..providers import ModelProvider, ProviderRegistry, ProviderSpec


class ApplicationContainer:
    """The one composition root for application and built-in extension objects."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.event_bus = RedisRuntimeEventBus(self.settings)
        self.event_writer = RuntimeEventBatchWriter(self.settings)
        self.task_manager = RuntimeTaskManager(
            runtime_event_bus=self.event_bus,
            runtime_event_writer=self.event_writer,
        )
        self.task_queue = DurableTaskQueue(self.settings)
        self.hook_runner = HookRunner(self.settings.hooks_config_path)
        self.auth_rate_limiter = AuthRateLimiter(self.settings)
        self.notification_dispatcher = NotificationDispatcher(self.settings)
        self.context_manager = ConversationContextManager(self.settings)
        self.memory_service = MemoryService()
        self.memory_job_queue = DurableMemoryJobQueue(self.settings, memory=self.memory_service)
        self.github_integration = GitHubIntegration()
        self.mcp_runtime = MCPClientRuntime()
        self.project_skill_runtime = ProjectSkillRuntime()
        self.project_context_loader = ProjectContextLoader(self.project_skill_runtime)
        self.remote_execution_service = RemoteExecutionService()
        self.terminal_manager = WorkspaceTerminalManager()
        self.lsp_manager = WorkspaceLspManager()
        self.rag_store = LocalRagStore()
        self.code_executor = CodeExecutor()
        self.attachment_store = ChatAttachmentStore()
        self.workspace_editor = WorkspaceEditor(lsp_manager=self.lsp_manager)
        self.workspace_manager = ConversationWorkspaceManager(
            editor_factory=lambda root: WorkspaceEditor(root, lsp_manager=self.lsp_manager)
        )
        self.conversations = ConversationService(self.workspace_manager, self.context_manager)
        self.provider_registry = self._provider_registry()
        self.chat_provider = self.provider_registry.create(self.settings.chat_provider_id)
        self.coding_provider = self.provider_registry.create(self.settings.coding_provider_id)
        self.tool_catalog = ProductToolRegistry(
            self.rag_store, self.code_executor, self.workspace_editor,
            tavily_api_key=self.settings.tavily_api_key,
        )
        self.extension_registry = self._extension_registry()
        self.runtime_host = PlatformAgentRuntimeHost(hooks=self.hook_runner, tasks=self.task_manager)
        self.extension_services = {
            "github": self.github_integration,
            "mcp": self.mcp_runtime,
            "memory": self.memory_service,
            "project_context": self.project_context_loader,
            "rag_intent_router": route_rag_query,
            "remote_execution": self.remote_execution_service,
            "task_manager": self.task_manager,
            "runtime_host": self.runtime_host,
            "settings": self.settings,
        }
        config = ProductRuntimeConfig(
            chat_model_id=self.settings.chat_model_id,
            coding_model_id=self.settings.coding_model_id,
            fallback_model_id=self.settings.deepseek_model,
            max_agent_turns=self.settings.max_agent_turns,
            context_window=self.settings.model_context_tokens,
            max_output_tokens=self.settings.model_max_tokens,
            context_compaction=self.settings.llm_context_compaction,
            context_compaction_trigger_ratio=self.settings.context_compaction_trigger_ratio,
            chat_extensions=self.settings.chat_extensions,
            coding_extensions=self.settings.coding_extensions,
        )
        self.chatbot = ChatbotMode(
            provider=self.chat_provider, rag=self.rag_store, tools=self.tool_catalog,
            extensions=self.extension_registry, runtime_host=self.runtime_host,
            extension_services=self.extension_services, config=config,
        )
        self.coding_agent = CodingAgentMode(
            provider=self.coding_provider, tools=self.tool_catalog,
            extensions=self.extension_registry, runtime_host=self.runtime_host,
            extension_services=self.extension_services, config=config,
            workspace_factory=lambda root: WorkspaceEditor(root, lsp_manager=self.lsp_manager),
        )
        self.product_gateway = ProductGateway(chatbot=self.chatbot, coding_agent=self.coding_agent)
        self.automation_scheduler = AutomationScheduler(
            self.conversations, task_manager=self.task_manager, queue=self.task_queue,
            poll_seconds=self.settings.automation_poll_seconds,
        )

    def _provider_registry(self) -> ProviderRegistry:
        common = {
            "max_tokens": self.settings.model_max_tokens,
            "temperature": self.settings.model_temperature,
            "request_timeout_seconds": self.settings.model_request_timeout_seconds,
        }
        return ProviderRegistry((
            ProviderSpec(
                id="deepseek", base_url=self.settings.deepseek_base_url,
                api_key=self.settings.deepseek_api_key, default_model=self.settings.deepseek_model,
                input_cost_per_million=self.settings.model_input_cost_per_million,
                output_cost_per_million=self.settings.model_output_cost_per_million,
                capabilities=("text", "streaming", "tools", "reasoning"),
                thinking_mode=self.settings.deepseek_thinking_mode,
                reasoning_effort=self.settings.deepseek_reasoning_effort,
                **common,
            ),
            ProviderSpec(
                id="anthropic", base_url=self.settings.anthropic_base_url,
                api_key=self.settings.anthropic_api_key,
                auth_token=self.settings.anthropic_auth_token,
                default_model=self.settings.anthropic_model,
                capabilities=("text", "streaming", "tools", "reasoning"), protocol="anthropic",
                **common,
            ),
        ), providers_json=self.settings.model_providers_json)

    @staticmethod
    def _extension_registry() -> ExtensionRegistry:
        registry = ExtensionRegistry()
        for extension in (
            RagExtension(), WebSearchExtension(), ChatMemoryExtension(), WorkspaceExtension(),
            DiagnosticsExtension(), GitExtension(), TerminalExtension(), ProjectContextExtension(),
            MCPExtension(), GitHubExtension(), RemoteExecutionExtension(), PlanExtension(),
            ProjectMemoryExtension(), SubagentExtension(),
        ):
            registry.register(extension)
        return registry

    def provider(self, product: str = "coding") -> ModelProvider:
        return self.chat_provider if product == "chat" else self.coding_provider

    async def execute_task(self, task_id: str) -> bool:
        return await execute_queued_task(
            task_id, gateway=self.product_gateway, conversations=self.conversations,
            memory_jobs=self.memory_job_queue, task_manager=self.task_manager,
        )

    async def execute_memory_job(self, job_id: str) -> bool:
        return await execute_memory_job(
            job_id,
            memory=self.memory_service,
            tasks=self.task_manager,
            queue=self.memory_job_queue,
            chat_provider=self.chat_provider,
            coding_provider=self.coding_provider,
            settings=self.settings,
        )

    async def close(self) -> None:
        self.terminal_manager.close_all()
        self.lsp_manager.close_all()
        await self.mcp_runtime.close()
        self.event_writer.close()


@lru_cache(maxsize=1)
def get_container() -> ApplicationContainer:
    return ApplicationContainer()


def reset_container() -> None:
    """Drop the cached graph. Intended for tests and controlled reloads."""
    get_container.cache_clear()
